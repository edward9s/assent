"""Transactional local acceptance of one reviewed work folder.

``assent accept FOLDER`` records a human acceptance decision as a local Git
integration. It deliberately has no batch, push, remote, or hosting behavior.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from assent import AssentError, gitops
from assent.config import Config
from assent.folderdeps import parse_folder_dependency_graph
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan

_DEFAULT_VERIFY_COMMAND = "python .assent/verify.py"
_COMPLETE_STATUSES = ("DONE", "SKIP")


def _resolvable_source_tip(main: Path, folder: str) -> str | None:
    """Return an unambiguous source tip, or None so evidence can be tried."""
    worktree = gitops.folder_worktree(main, folder)
    if worktree is not None:
        branch = gitops.current_branch(worktree)
        if not branch or not branch.startswith(f"{folder}/"):
            return None
        return gitops.branch_tip(main, branch)
    branches = gitops.folder_branches(main, folder)
    if len(branches) != 1:
        return None
    return gitops.branch_tip(main, branches[0])


def folder_present_in_target(main: Path, folder: str, target_branch: str,
                             target_head: str) -> bool:
    """Prove folder presence by source ancestry or durable accept evidence."""
    tip = _resolvable_source_tip(main, folder)
    if tip is not None and gitops.is_ancestor(main, tip, target_head):
        return True
    return (gitops.find_accept_evidence(main, folder, ref=target_branch).status
            is gitops.AcceptStatus.ACCEPTED)


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
    """Run every precheck and trial operation before advancing the target."""
    folder = cfg.tasks_name

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in _COMPLETE_STATUSES]
    if unfinished:
        print(f"accept {folder}: refused, the folder is not finished "
              f"({', '.join(unfinished)}); every task must be DONE or SKIP")
        return 1
    graph = parse_folder_dependency_graph(cfg.assent_dir)

    main = gitops.main_worktree(cfg.root)
    target_branch = gitops.require_current_branch(main)
    if not gitops.working_tree_status(main).is_clean:
        print(f"accept {folder}: refused, the main worktree {main} is not clean; "
              "commit or set aside its changes before accepting")
        return 1
    target_before = gitops.commit_of(main, "HEAD")

    pending = [dependency for dependency in graph[folder].after
               if not folder_present_in_target(
                   main, dependency, target_branch, target_before)]
    if pending:
        print(f"accept {folder}: refused, prerequisite folder(s) not yet accepted into "
              f"{target_branch}: {', '.join(pending)}")
        return 1

    worktree = gitops.folder_worktree(main, folder)
    if worktree is not None:
        source_branch = gitops.current_branch(worktree)
        if not source_branch:
            print(f"accept {folder}: refused, its worktree {worktree} is in detached "
                  "HEAD state, so there is no source branch to accept")
            return 1
        if not source_branch.startswith(f"{folder}/"):
            print(f"accept {folder}: refused, its worktree {worktree} is on branch "
                  f"{source_branch}, which is not a {folder}/* branch")
            return 1
        if not gitops.working_tree_status(worktree).is_clean:
            print(f"accept {folder}: refused, the source worktree {worktree} is not clean")
            return 1
    else:
        branches = gitops.folder_branches(main, folder)
        if len(branches) > 1:
            print(f"accept {folder}: refused, multiple candidate source branches exist "
                  f"({', '.join(branches)}); accept does not guess which one is current")
            return 1
        if not branches:
            return _accept_already_cleaned(main, folder, target_branch)
        source_branch = branches[0]
    source_tip = gitops.branch_tip(main, source_branch)

    if gitops.is_ancestor(main, source_tip, target_before):
        print(f"accept {folder}: already accepted; {source_branch} "
              f"({source_tip[:12]}) is already contained in {target_branch}. "
              "Nothing to do.")
        return 0

    commands = _verify_commands(plan)
    ok, failed = _verify_source(cfg, main, worktree, source_tip, commands)
    if not ok:
        print(f"accept {folder}: refused, source verification failed ({failed}); "
              f"{target_branch} left unchanged")
        return 1
    if worktree is not None:
        if (gitops.current_branch(worktree) != source_branch
                or gitops.branch_tip(main, source_branch) != source_tip
                or not gitops.working_tree_status(worktree).is_clean):
            print(f"accept {folder}: refused, the source changed during verification; "
                  f"{target_branch} left unchanged")
            return 1

    subject = f"accept({folder}): integrate into {target_branch}"
    message = gitops.accept_commit_message(
        subject, folder, source_branch, source_tip)
    with gitops.temporary_integration_worktree(
            main, folder, target_before) as (integration, integration_branch):
        outcome = gitops.merge_no_ff(integration, source_tip, message)
        if not outcome.ok:
            print(f"accept {folder}: refused, merging {source_branch} into "
                  f"{target_branch} conflicts. accept never auto-resolves; "
                  f"{target_branch} left unchanged. Conflicting file(s):")
            for path in outcome.conflicts:
                print(f"  - {path}")
            return 1

        ok, failed = _run_verifies(cfg, integration, commands)
        if not ok:
            print(f"accept {folder}: refused, post-merge verification failed "
                  f"({failed}); {target_branch} left unchanged, source "
                  "branch/worktree kept")
            return 1

        integration_commit = gitops.commit_of(main, integration_branch)
        if gitops.current_branch(main) != target_branch:
            print(f"accept {folder}: refused, the main worktree {main} is no longer "
                  f"on {target_branch} (concurrent branch switch during accept); "
                  f"{target_branch} left unchanged")
            return 1
        if gitops.commit_of(main, "HEAD") != target_before:
            print(f"accept {folder}: refused, {target_branch} moved during accept "
                  "(concurrent update); refusing to overwrite it. Target left as it "
                  "now stands.")
            return 1
        if not gitops.working_tree_status(main).is_clean:
            print(f"accept {folder}: refused, the main worktree {main} became dirty "
                  f"during accept; {target_branch} left unchanged")
            return 1
        gitops.fast_forward(main, integration_commit)

    target_after = gitops.commit_of(main, "HEAD")
    print(f"accept {folder}: done, verification passed.")
    print(f"  folder:       {folder}")
    print(f"  source branch: {source_branch}")
    print(f"  source tip:    {source_tip}")
    print(f"  target branch: {target_branch}")
    print(f"  target before: {target_before}")
    print(f"  target after:  {target_after}")
    print(f"  evidence merge commit: {integration_commit}")
    print("  source branch/worktree kept; `assent clean` can remove them once merged.")
    print("  The integration lock coordinates Assent commands only; do not run "
          "concurrent external Git writes during acceptance.")
    return 0


def _accept_already_cleaned(main: Path, folder: str,
                            target_branch: str) -> int:
    """Succeed without a source only when target history proves acceptance."""
    result = gitops.find_accept_evidence(main, folder, ref=target_branch)
    if result.status is gitops.AcceptStatus.ACCEPTED:
        evidence = result.evidence
        print(f"accept {folder}: already accepted and cleaned; {target_branch} "
              f"records source {evidence.source_branch} "
              f"({evidence.source_tip[:12]}). Nothing to do.")
        return 0
    print(f"accept {folder}: refused, no source worktree or {folder}/* branch "
          f"exists, and {target_branch}'s history holds no trustworthy accept "
          f"evidence for {folder}")
    return 1


def _verify_commands(plan: Plan) -> list[str]:
    """Collect unique DONE-task verification commands in filename order."""
    seen: set[str] = set()
    commands: list[str] = []
    for task in plan.tasks:
        if task.status == "DONE" and task.verify not in seen:
            seen.add(task.verify)
            commands.append(task.verify)
    return commands


def _verify_source(cfg: Config, main: Path, worktree: Path | None,
                   source_tip: str,
                   commands: list[str]) -> tuple[bool, str | None]:
    if worktree is not None:
        return _run_verifies(cfg, worktree, commands)
    with gitops.temporary_source_worktree(main, source_tip) as snapshot:
        return _run_verifies(cfg, snapshot, commands)


def _run_verifies(cfg: Config, tree: Path,
                  commands: list[str]) -> tuple[bool, str | None]:
    for command in commands:
        if _run_verify(cfg, tree, command) != 0:
            return False, command
    return True, None


def _run_verify(cfg: Config, tree: Path, command: str) -> int:
    """Run one task verification in the requested source or merge snapshot."""
    if command.strip() == _DEFAULT_VERIFY_COMMAND:
        result = subprocess.run(
            [sys.executable, str((cfg.assent_dir / "verify.py").resolve())],
            cwd=str(tree), capture_output=True, encoding="utf-8",
            errors="replace")
    else:
        result = subprocess.run(
            command, shell=True, cwd=str(tree), capture_output=True,
            encoding="utf-8", errors="replace")
    return result.returncode
