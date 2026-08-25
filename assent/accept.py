"""Fast transactional publication of one independently verified plan.

``assent accept PLAN`` never runs a verifier.  It reconstructs the current
integration candidate and publishes it only when its exact tree matches a
PASSED verification receipt for the current source and verifier.

This module owns that one direct transaction and nothing else.  The batch
paths -- the explicit selected ``accept A B``, the batch release, and
``accept --all`` -- live in ``assent.batch_accept``, which reuses
``accept_plan`` and the three deliberately public helpers below.  The
dependency runs that way only, so either safety-sensitive path can be read and
changed without loading the other.
"""

from __future__ import annotations

from pathlib import Path

from typing import Sequence

from assent import AssentError, gitops, verification

from assent.config import Config

from assent.plan_source import COMPLETE_STATUSES, resolve_source_snapshot

from assent.plandeps import (infer_plan_completion, live_upstreams,
                               parse_plan_dependencies)

from assent.lockfile import LockBusy, hold_integration_lock, hold_lock

from assent.plan import Plan

def dependency_tip(main: Path, plan_name: str,
                   excludes: Sequence[str] = ()) -> tuple[str | None, str | None]:
    """Resolve a direct prerequisite's current tip without parsing its task plan."""
    worktree = gitops.plan_worktree(main, plan_name)
    if worktree is not None:
        branch = gitops.current_branch(worktree)
        if not branch:
            return None, f"its source worktree {worktree} is detached"
        if not branch.startswith(f"{plan_name}/") or branch == f"{plan_name}/":
            return None, f"its source worktree is on non-{plan_name} branch {branch}"
        if not gitops.working_tree_status(worktree, excludes).is_clean:
            return None, "its source worktree is dirty"
        tip = gitops.branch_tip(main, branch)
        if (gitops.commit_of(worktree, "HEAD") != tip
                or gitops.branch_tip(main, branch) != tip):
            return None, "its source changed while its tip was being resolved"
        return tip, None
    branches = gitops.plan_branches(main, plan_name)
    if not branches:
        return None, "its source was cleaned before the dependent was accepted"
    if len(branches) > 1:
        return None, f"its current source is ambiguous ({', '.join(branches)})"
    return gitops.branch_tip(main, branches[0]), None

def _refresh_message(plan_name: str, reason: str) -> str:
    return (f"accept {plan_name}: refused, {reason}. Run `assent verify {plan_name}` "
            "to refresh the verification receipt unattended; accept never runs "
            "the full verifier")

def accept_merge_message(target_branch: str, plan_name: str, source_branch: str,
                         source_tip: str, verified_tree: str,
                         verifier_sha256: str) -> str:
    """Compose the one accept merge message, whether published alone or in a batch.

    A batch release publishes exactly the merges a plan-by-plan accept would
    have, so audit granularity must not become coarser just because several
    plans were verified together.  Both paths build their message here so the
    subject and the evidence trailers cannot drift apart.
    """
    subject = f"accept({plan_name}): integrate into {target_branch}"
    return gitops.accept_commit_message(
        subject, plan_name, source_branch, source_tip, verified_tree,
        verifier_sha256)

def cleanup_warning(path: Path, branch: str, error: AssentError) -> None:
    print(f"warning: {error}")
    print("warning: acceptance cleanup may require manual recovery:")
    print(f"  temporary ref:  refs/heads/{branch}")
    print(f"  temporary path: {path}")

def accept_plan(cfg: Config) -> int:
    """Accept exactly ``cfg.tasks_name``; return zero only on proven success."""
    plan_name = cfg.tasks_name
    try:
        # This order is part of the concurrency contract and must remain fixed.
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, plan_name):
                return _accept_locked(cfg)
    except LockBusy as e:
        print(f"accept {plan_name}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"accept {plan_name}: failed ({e})")
        return 1

def _accept_locked(cfg: Config) -> int:
    """Rebuild and publish an exact receipt-backed integration candidate."""
    plan_name = cfg.tasks_name

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in COMPLETE_STATUSES]
    if unfinished:
        print(f"accept {plan_name}: refused, the plan is not finished "
              f"({', '.join(unfinished)}); every task must be DONE or SKIP")
        return 1
    dependencies = parse_plan_dependencies(cfg.tasks_dir)

    main = gitops.main_worktree(cfg.root)
    target_branch = gitops.require_current_branch(main)
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        print(f"accept {plan_name}: refused, the main worktree {main} is not clean; "
              "commit or set aside its changes before accepting")
        return 1
    target_before = gitops.commit_of(main, "HEAD")

    pending: list[str] = []
    dependency_tips: list[tuple[str, str]] = []
    # An archived upstream is already integrated into the target and its source
    # is gone by design, so requiring a live tip for it would refuse a
    # legitimate accept (see live_upstreams).
    for dependency in live_upstreams(cfg.assent_dir, dependencies):
        completion = infer_plan_completion(cfg.assent_dir / dependency)
        if not completion.complete:
            pending.append(f"{dependency} ({completion.reason})")
            continue
        tip, problem = dependency_tip(main, dependency, cfg.git_excludes)
        if problem is not None:
            pending.append(f"{dependency} ({problem})")
        elif not gitops.is_ancestor(main, tip, target_before):
            pending.append(f"{dependency} (current tip {tip[:12]} is not in target)")
        else:
            dependency_tips.append((dependency, tip))
    if pending:
        print(f"accept {plan_name}: refused, prerequisite plan(s) cannot be proven "
              f"accepted into {target_branch}: {', '.join(pending)}. Do not clean "
              "an upstream source before its dependents are accepted")
        return 1

    try:
        source_branch, source_tip, source_worktree = resolve_source_snapshot(
            main, plan_name, cfg.git_excludes)
    except AssentError as e:
        print(f"accept {plan_name}: refused, {e}")
        return 1

    if gitops.is_ancestor(main, source_tip, target_before):
        print(f"accept {plan_name}: already accepted; current source {source_branch} "
              f"({source_tip[:12]}) is fully contained in {target_branch}. "
              "Nothing to do.")
        return 0

    stale_dependencies = [
        f"{dependency} ({tip[:12]})"
        for dependency, tip in dependency_tips
        if dependencies.base == dependency
        and not gitops.is_ancestor(main, tip, source_tip)
    ]
    if stale_dependencies:
        print(f"accept {plan_name}: refused, the downstream source does not contain "
              f"the current accepted prerequisite tip(s): "
              f"{', '.join(stale_dependencies)}. The target, source, and receipt "
              f"were preserved; run `assent rework {plan_name}` or replan the "
              "dependency")
        return 1

    try:
        receipt = verification.read_receipt(
            verification.receipt_path(cfg), main)
    except AssentError as e:
        print(_refresh_message(plan_name, str(e)))
        return 1
    if receipt.status != "PASSED":
        print(_refresh_message(
            plan_name, f"verification receipt status is {receipt.status}"))
        return 1
    if receipt.source_tip != source_tip:
        print(_refresh_message(
            plan_name, "source tip changed since verification "
            f"({receipt.source_tip} -> {source_tip})"))
        return 1
    try:
        current_digest = verification.verifier_digest(cfg)
    except AssentError as e:
        print(_refresh_message(plan_name, str(e)))
        return 1
    if receipt.verify_script_sha256 != current_digest:
        print(_refresh_message(plan_name, "the verification script changed"))
        return 1
    # Ignored-directory inputs are evidence like the source, target, and verifier are: a
    # changed profile, declared target, or target content means the receipt no
    # longer describes what would be tested.  Accept never repairs a link or
    # invokes AI to make this pass; it refuses and asks for a fresh verify.
    try:
        current_ignored_dirs = verification.current_ignored_directory_inputs(cfg)
    except AssentError as e:
        print(_refresh_message(plan_name, str(e)))
        return 1
    if receipt.ignored_directory_inputs_sha256 != current_ignored_dirs:
        print(_refresh_message(
            plan_name, "the reviewed ignored-directory inputs changed since verification"))
        return 1

    message = accept_merge_message(
        target_branch, plan_name, source_branch, source_tip,
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
                main, plan_name, target_before) as (candidate, candidate_branch):
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
                else:
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
                        for dependency, accepted_tip in dependency_tips:
                            current_tip, problem = dependency_tip(
                                main, dependency, cfg.git_excludes)
                            if problem is not None or current_tip != accepted_tip:
                                detail = problem or (
                                    f"tip changed from {accepted_tip} to "
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
                            try:
                                final_ignored_dirs = (
                                    verification.current_ignored_directory_inputs(cfg))
                            except AssentError as e:
                                gate_problem = str(e)
                            else:
                                if final_ignored_dirs != current_ignored_dirs:
                                    gate_problem = (
                                        "the reviewed ignored-directory inputs changed during "
                                        "acceptance")
                        if gate_problem is None:
                            gitops.fast_forward(main, integration_commit)
                            published = True
            body_completed = True
    except AssentError as cleanup_error:
        if not body_completed:
            notes = getattr(cleanup_error, "__notes__", ())
            if notes and integration is not None and integration_branch is not None:
                cleanup_warning(
                    integration, integration_branch,
                    AssentError("; ".join(str(note) for note in notes)))
            raise
        assert integration is not None and integration_branch is not None
        cleanup_warning(integration, integration_branch, cleanup_error)
        if not published and gate_problem is None and not conflict_paths:
            return 1

    if conflict_paths:
        print(f"accept {plan_name}: refused, merging {source_branch} into "
              f"{target_branch} conflicts. accept never auto-resolves; "
              "accept did not change the target. Conflicting file(s):")
        for path in conflict_paths:
            print(f"  - {path}")
        print(f"Run `assent reconcile {plan_name}` to resolve the source-versus-"
              "target conflict in an isolated worktree, then `assent verify "
              f"{plan_name}` to earn a fresh receipt")
        return 1
    if gate_problem is not None:
        print(_refresh_message(plan_name, gate_problem))
        print("accept did not publish; target refs/worktree remain as currently "
              "observed, and the source branch/worktree was kept")
        return 1
    target_after = gitops.commit_of(main, "HEAD")
    print(f"accept {plan_name}: done, PASSED receipt tree published without running verification.")
    print(f"  plan:            {plan_name}")
    print(f"  source branch:   {source_branch}")
    print(f"  source tip:      {source_tip}")
    print(f"  verified tree:   {receipt.integration_tree}")
    print(f"  verifier digest: {receipt.verify_script_sha256}")
    print(f"  target branch:   {target_branch}")
    print(f"  target before:   {target_before}")
    print(f"  target after:    {target_after}")
    print(f"  audit merge:     {integration_commit}")
    print("  source branch/worktree kept; retain it while a dependent may still need "
          f"its source evidence. `assent clean {plan_name}` makes the final safety decision.")
    print("  The integration lock coordinates Assent commands only; do not run "
          "concurrent external Git writes during acceptance.")
    return 0
