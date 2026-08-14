"""Fast transactional publication of one independently verified work folder.

``assent accept FOLDER`` never runs a verifier.  It reconstructs the current
integration candidate and publishes it only when its exact tree matches a
PASSED verification receipt for the current source and verifier.

This module owns that one direct transaction and nothing else.  The batch
paths -- the explicit selected ``accept A B``, the batch release, and
``accept --all`` -- live in ``assent.batch_accept``, which reuses
``accept_folder`` and the three deliberately public helpers below.  The
dependency runs that way only, so either safety-sensitive path can be read and
changed without loading the other.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

from assent import AssentError, auto_fix, gitops, verification
from assent.config import Config
from assent.folder_source import COMPLETE_STATUSES, resolve_source_snapshot
from assent.folderdeps import (infer_folder_completion, live_upstreams,
                               parse_folder_dependencies)
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan


def dependency_tip(main: Path, folder: str,
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


def accept_merge_message(target_branch: str, folder: str, source_branch: str,
                         source_tip: str, verified_tree: str,
                         verifier_sha256: str) -> str:
    """Compose the one accept merge message, whether published alone or in a batch.

    A batch release publishes exactly the merges a folder-by-folder accept would
    have, so audit granularity must not become coarser just because several
    folders were verified together.  Both paths build their message here so the
    subject and the evidence trailers cannot drift apart.
    """
    subject = f"accept({folder}): integrate into {target_branch}"
    return gitops.accept_commit_message(
        subject, folder, source_branch, source_tip, verified_tree,
        verifier_sha256)


def cleanup_warning(path: Path, branch: str, error: AssentError) -> None:
    print(f"warning: {error}")
    print("warning: acceptance cleanup may require manual recovery:")
    print(f"  temporary ref:  refs/heads/{branch}")
    print(f"  temporary path: {path}")


def _self_fixed_reason(folder: str, outcome: auto_fix.SelfFixedOutcome
                       ) -> tuple[str, list[str]]:
    """Describe the missing independent confirmation a human is asked to supply."""
    return "SELF-FIXED, UNREVIEWED", [
        f"  self-fixed round: {outcome.round_index + 1} of "
        f"{outcome.rounds_used} "
        f"({outcome.adapter}/{outcome.model}/{outcome.effort}); "
        "no later configured round confirmed the repair",
        "  Every task passed its own focused gate and the PASSED receipt "
        f"matches, but no independent review confirmed the last repair. See "
        f".assent/{folder}/_report.md for the findings it repaired."]


def _unresolved_reason(folder: str, state: auto_fix.AutoFixState,
                       outcome: auto_fix.UnresolvedReviewOutcome
                       ) -> tuple[str, list[str]]:
    """Describe every finding the human is being asked to overrule, not a count.

    The round list ended on blockers no round repaired, so the decision is the
    findings themselves.  A human cannot consent to a decision they cannot see,
    so each one is printed with its task, path and summary.
    """
    ledger = {item.fingerprint: item for item in state.findings}
    lines = [f"  unresolved review round: {outcome.round_index + 1} of "
             f"{outcome.rounds_used} "
             f"({outcome.adapter}/{outcome.model}/{outcome.effort}); "
             f"{len(outcome.finding_fingerprints)} finding(s) no configured "
             "round resolved:"]
    for fingerprint in outcome.finding_fingerprints:
        finding = ledger[fingerprint]
        lines.append(f"    - {finding.task_id or 'unassigned'} "
                     f"{finding.path}: {finding.summary}")
    lines.append(
        "  Every task keeps the status its own closeout gave it and the PASSED "
        "receipt matches, but the findings above were never resolved. See "
        f".assent/{folder}/_report.md.")
    return "REVIEW UNRESOLVED, HUMAN DECISION", lines


def _settled_reasons(folder: str, state: auto_fix.AutoFixState
                     ) -> tuple[list[str], list[str]]:
    """Collect every settled outcome a human must overrule, as one decision."""
    reasons: list[tuple[str, list[str]]] = []
    if state.self_fixed_unreviewed is not None:
        reasons.append(_self_fixed_reason(folder, state.self_fixed_unreviewed))
    if state.unresolved_review is not None:
        reasons.append(
            _unresolved_reason(folder, state, state.unresolved_review))
    labels = [label for label, _lines in reasons]
    lines = [line for _label, reason in reasons for line in reason]
    return labels, lines


def _confirm_settled(folder: str, labels: list[str], lines: list[str],
                     confirm: Callable[[str], str] | None) -> bool:
    """Ask once before publishing a folder the finite review loop never settled.

    The receipt-based evidence is complete, so this is not a refusal: what is
    missing is a decision only a human can make, whether that is the
    confirmation the round list ran out of, findings no round resolved, or
    both.  Two prompts for one decision train a human to answer without
    reading, so every reason is named in one prompt.  Anything other than
    exactly "y"/"Y", including EOF (no TTY or closed stdin), declines.
    """
    print(f"accept {folder}: the plan auto-fix state is "
          f"{' and '.join(labels)}.")
    for line in lines:
        print(line)
    ask = confirm if confirm is not None else input
    try:
        answer = ask("Publish it anyway? [y/N]: ")
    except EOFError:
        answer = ""
    return answer.strip().lower() == "y"


def _settled_state(cfg: Config) -> auto_fix.AutoFixState | None:
    """Read the settled auto-fix outcome from deletable folder runtime memory."""
    path = auto_fix.auto_fix_state_path(cfg)
    if not path.is_file():
        return None
    try:
        return auto_fix.read_auto_fix_state(path)
    except AssentError:
        # The state file is derived, deletable memory, never acceptance
        # evidence: a malformed record cannot manufacture a second gate on a
        # folder whose receipt evidence is complete.
        return None


def accept_folder(cfg: Config, confirm: Callable[[str], str] | None = None) -> int:
    """Accept exactly ``cfg.tasks_name``; return zero only on proven success."""
    folder = cfg.tasks_name
    try:
        # This order is part of the concurrency contract and must remain fixed.
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, folder):
                return _accept_locked(cfg, confirm)
    except LockBusy as e:
        print(f"accept {folder}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"accept {folder}: failed ({e})")
        return 1


def _accept_locked(cfg: Config,
                   confirm: Callable[[str], str] | None = None) -> int:
    """Rebuild and publish an exact receipt-backed integration candidate."""
    folder = cfg.tasks_name

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in COMPLETE_STATUSES]
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
    # An archived upstream is already integrated into the target and its source
    # is gone by design, so requiring a live tip for it would refuse a
    # legitimate accept (see live_upstreams).
    for dependency in live_upstreams(cfg.assent_dir, dependencies):
        completion = infer_folder_completion(cfg.assent_dir / dependency)
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
        print(f"accept {folder}: refused, prerequisite folder(s) cannot be proven "
              f"accepted into {target_branch}: {', '.join(pending)}. Do not clean "
              "an upstream source before its dependents are accepted")
        return 1

    try:
        source_branch, source_tip, source_worktree = resolve_source_snapshot(
            main, folder, cfg.git_excludes)
    except AssentError as e:
        print(f"accept {folder}: refused, {e}")
        return 1

    if gitops.is_ancestor(main, source_tip, target_before):
        print(f"accept {folder}: already accepted; current source {source_branch} "
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
    # Shared inputs are evidence like the source, target, and verifier are: a
    # changed profile, declared target, or target content means the receipt no
    # longer describes what would be tested.  Accept never repairs a link or
    # invokes AI to make this pass; it refuses and asks for a fresh verify.
    try:
        current_shared = verification.current_shared_inputs(cfg)
    except AssentError as e:
        print(_refresh_message(folder, str(e)))
        return 1
    if receipt.shared_inputs_sha256 != current_shared:
        print(_refresh_message(
            folder, "the reviewed shared inputs changed since verification"))
        return 1

    # The last gate before the merge, and the only one a human can answer: every
    # receipt-based check above already passed, so a decline here is a human
    # withholding the confirmation the finite review loop never produced.
    settled = _settled_state(cfg)
    if settled is not None:
        labels, lines = _settled_reasons(folder, settled)
        if labels and not _confirm_settled(folder, labels, lines, confirm):
            print(f"accept {folder}: refused, publishing a "
                  f"{' and '.join(labels)} folder was not confirmed; nothing "
                  "was merged and no Git state changed. Review it, then run "
                  f"`assent accept {folder}` again and answer y to publish it")
            return 1

    message = accept_merge_message(
        target_branch, folder, source_branch, source_tip,
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
                                final_shared = verification.current_shared_inputs(cfg)
                            except AssentError as e:
                                gate_problem = str(e)
                            else:
                                if final_shared != current_shared:
                                    gate_problem = (
                                        "the reviewed shared inputs changed during "
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
        print(f"accept {folder}: refused, merging {source_branch} into "
              f"{target_branch} conflicts. accept never auto-resolves; "
              "accept did not change the target. Conflicting file(s):")
        for path in conflict_paths:
            print(f"  - {path}")
        print(f"Run `assent reconcile {folder}` to resolve the source-versus-"
              "target conflict in an isolated worktree, then `assent verify "
              f"{folder}` to earn a fresh receipt")
        return 1
    if gate_problem is not None:
        print(_refresh_message(folder, gate_problem))
        print("accept did not publish; target refs/worktree remain as currently "
              "observed, and the source branch/worktree was kept")
        return 1
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
