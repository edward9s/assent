"""Fast transactional publication of one independently verified work folder.

``assent accept FOLDER`` never runs a verifier.  It reconstructs the current
integration candidate and publishes it only when its exact tree matches a
PASSED verification receipt for the current source and verifier.

``assent accept --all`` (``accept_all`` below) is the only caller that may
run a verifier: for each finished folder in dependency order it refreshes a
stale receipt first, then publishes through the same ``accept_folder``.

``accept --all`` also owns the batch release path.  When a fresh PASSED batch
receipt covers several folders, their whole merge chain is replayed in one
temporary worktree, compared tree by tree against that receipt, and published
by a single ref update -- all of them or none of them.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from assent import AssentError, gitops, verification
from assent.config import Config, load_config
from assent.folderdeps import (infer_folder_completion, live_upstreams,
                               order_folders_by_dependency,
                               parse_folder_dependencies,
                               parse_folder_dependency_graph)
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan

_COMPLETE_STATUSES = ("DONE", "SKIP")

# Both --all paths announce themselves before doing any work, so which one a run
# took is always readable from its output instead of having to be inferred.
_BATCH_BANNER = (
    "accept --all: batch release; the whole batch is published by one ref "
    "update or not at all (all or nothing on failure).")
_PER_FOLDER_BANNER = (
    "accept --all: per-folder verify+accept; on failure the chain stops and "
    "the folders already published stay published.")


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


def _accept_merge_message(target_branch: str, folder: str, source_branch: str,
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
    # An archived upstream is already integrated into the target and its source
    # is gone by design, so requiring a live tip for it would refuse a
    # legitimate accept (see live_upstreams).
    for dependency in live_upstreams(cfg.assent_dir, dependencies):
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

    if gitops.is_ancestor(main, source_tip, target_before):
        print(f"accept {folder}: already accepted; current source {source_branch} "
              f"({source_tip[:12]}) is fully contained in {target_branch}. "
              "Nothing to do.")
        return 0

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

    message = _accept_merge_message(
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


def _already_integrated(cfg: Config) -> bool:
    """Best-effort check for a folder whose current source is already an
    ancestor of the current target -- i.e. a prior accept already published
    it and nothing has changed since.

    This lets a rerun route straight to ``accept_folder``'s own idempotent
    "Nothing to do" path instead of re-deriving a fresh candidate: rebuilding
    a merge whose source is already fully contained is a Git no-op, not a
    two-parent commit, which the verify-side freshness rebuild does not
    expect. Any ambiguity here (no source, detached, ...) is not this
    function's job to diagnose, so it defers to the normal verify/accept
    path, which reports it precisely.
    """
    try:
        main = gitops.main_worktree(cfg.root)
        target_tip = gitops.commit_of(main, gitops.require_current_branch(main))
        _branch, source_tip, _worktree = _source_snapshot(
            main, cfg.tasks_name, cfg.git_excludes)
    except AssentError:
        return False
    return gitops.is_ancestor(main, source_tip, target_tip)


def _no_source_remains(main: Path, folder: str) -> bool:
    """True when a folder's ``<folder>/*`` worktree and branches are both gone.

    ``assent clean`` only deletes a folder's branch once it has proven the
    branch is fully merged into the integration target, so the branch's
    absence is itself the merge evidence -- ``accept --all`` does not need a
    fresh proof to skip it.  A branch deleted by hand outside ``clean`` is not
    covered by this contract, matching ``clean``'s own existing stance.
    """
    return (gitops.folder_worktree(main, folder) is None
            and not gitops.folder_branches(main, folder))


@dataclass(frozen=True)
class _BatchSource:
    """One batched folder's live Git identity beside its recorded step tree."""

    folder: str
    branch: str
    tip: str
    worktree: Path | None
    step_tree: str


@dataclass
class _BatchReplay:
    """What replaying the recorded chain produced, before anything was published."""

    merges: list[tuple[str, str]] = field(default_factory=list)
    chain_head: str = ""
    problem: str = ""
    conflict_folder: str = ""
    conflicts: tuple[str, ...] = ()
    published: bool = False


def _batch_source_identities(
        main: Path, receipt: verification.BatchVerificationReceipt,
        configs: dict[str, Config]) -> tuple[list[_BatchSource], str | None]:
    """Resolve every batched folder's current source, refusing on any drift.

    ``_source_snapshot`` is the same resolution a single-folder accept performs,
    so a detached, foreign, ambiguous, or dirty source refuses the batch for
    exactly the reasons it would refuse one folder.
    """
    sources: list[_BatchSource] = []
    for entry in receipt.sources:
        try:
            branch, tip, worktree = _source_snapshot(
                main, entry.folder, configs[entry.folder].git_excludes)
        except AssentError as e:
            return [], f"{entry.folder}: {e}"
        if tip != entry.source_tip:
            return [], (f"{entry.folder}: source tip changed since the batch was "
                        f"verified ({entry.source_tip[:12]} -> {tip[:12]})")
        sources.append(
            _BatchSource(entry.folder, branch, tip, worktree, entry.step_tree))
    return sources, None


def _batch_gate_problem(main: Path, configs: dict[str, Config],
                        sources: Sequence[_BatchSource], target_branch: str,
                        target_before: str, digest: str) -> str | None:
    """Re-observe every identity a batch release is about to publish over.

    This is the single-folder accept gate widened to cover all sources at once:
    the target must still be the branch and the commit the chain was replayed
    onto, the main worktree must still be clean, and every source branch and
    worktree must still be exactly what the receipt certified.  It runs both
    before and after the replay, so a concurrent change during the replay is
    caught while the target is still untouched.
    """
    main_excludes = configs[sources[0].folder].git_excludes
    if gitops.current_branch(main) != target_branch:
        return (f"the main worktree {main} is no longer on {target_branch} "
                "(concurrent branch switch during the batch release)")
    if gitops.commit_of(main, "HEAD") != target_before:
        return (f"{target_branch} moved during the batch release (concurrent "
                "update); refusing to overwrite it")
    if not gitops.working_tree_status(main, main_excludes).is_clean:
        return f"the main worktree {main} is not clean"
    for source in sources:
        excludes = configs[source.folder].git_excludes
        if gitops.branch_tip(main, source.branch) != source.tip:
            return (f"source branch {source.branch} moved away from the verified "
                    f"tip {source.tip[:12]}")
        if source.worktree is not None:
            if gitops.current_branch(source.worktree) != source.branch:
                return (f"source worktree {source.worktree} is no longer on "
                        f"{source.branch}")
            if not gitops.working_tree_status(source.worktree, excludes).is_clean:
                return f"source worktree {source.worktree} is not clean"
        # The selection and freshness rules already exclude a source the target
        # contains; asserting it here keeps a merged folder out of the replayed
        # chain, where its merge would collapse into a no-op commit shape.
        if gitops.is_ancestor(main, source.tip, target_before):
            return (f"{source.folder} ({source.tip[:12]}) is already contained in "
                    f"{target_branch}, so the recorded chain is not the chain "
                    "being published")
    try:
        current = verification.verifier_digest(configs[sources[0].folder])
    except AssentError as e:
        return str(e)
    if current != digest:
        return "the verification script changed during the batch release"
    return None


def _batch_prerequisite_problem(main: Path, assent_dir: Path,
                                configs: dict[str, Config],
                                sources: Sequence[_BatchSource],
                                target_before: str) -> str | None:
    """Refuse a batch that would publish a folder ahead of an upstream it needs.

    A single-folder accept refuses to publish a downstream whose prerequisite is
    not provably in the target.  A batch satisfies the same requirement in one of
    two ways: the upstream is earlier in this very chain, or it is already
    accepted.  Anything else would carry unverified upstream commits into the
    target through a downstream merge, which is exactly what the single-folder
    gate exists to prevent.
    """
    earlier: set[str] = set()
    for source in sources:
        excludes = configs[source.folder].git_excludes
        dependencies = parse_folder_dependencies(configs[source.folder].tasks_dir)
        for dependency in live_upstreams(assent_dir, dependencies):
            if dependency in earlier:
                continue
            tip, problem = _dependency_tip(main, dependency, excludes)
            if problem is not None:
                return (f"prerequisite {dependency} of {source.folder} cannot be "
                        f"proven accepted ({problem})")
            if not gitops.is_ancestor(main, tip, target_before):
                return (f"prerequisite {dependency} of {source.folder} "
                        f"({tip[:12]}) is neither part of this batch nor already "
                        "accepted into the target")
        earlier.add(source.folder)
    return None


def _replay_batch_chain(candidate: Path, target_branch: str,
                        receipt: verification.BatchVerificationReceipt,
                        sources: Sequence[_BatchSource],
                        replay: _BatchReplay) -> None:
    """Rebuild the verified chain in an open temporary worktree, step by step.

    Every step is compared against the receipt as soon as it exists, so a chain
    that has stopped being the verified one is abandoned before the next merge
    instead of at the end.  Trees do not depend on commit messages, so replaying
    with the audit messages a single-folder accept writes reproduces exactly the
    trees ``verify --batch`` recorded.
    """
    for source in sources:
        message = _accept_merge_message(
            target_branch, source.folder, source.branch, source.tip,
            source.step_tree, receipt.verify_script_sha256)
        previous = gitops.commit_of(candidate, "HEAD")
        outcome = gitops.merge_no_ff(candidate, source.tip, message)
        if not outcome.ok:
            replay.conflict_folder = source.folder
            replay.conflicts = outcome.conflicts
            return
        if gitops.commit_parents(candidate, "HEAD") != (previous, source.tip):
            replay.problem = (f"replaying {source.folder} did not produce the "
                              "expected two-parent no-ff merge")
            return
        tree = gitops.tree_of(candidate, "HEAD")
        if tree != source.step_tree:
            replay.problem = (f"replayed tree after {source.folder} is {tree}, "
                              f"not the verified {source.step_tree}")
            return
        replay.merges.append(
            (source.folder, gitops.commit_of(candidate, "HEAD")))
    final_tree = gitops.tree_of(candidate, "HEAD")
    if final_tree != receipt.final_tree:
        replay.problem = (f"replayed final tree is {final_tree}, not the verified "
                          f"{receipt.final_tree}")
        return
    replay.chain_head = gitops.commit_of(candidate, "HEAD")


def _unbatched_finished_folders(config_path: str, assent_dir: Path, main: Path,
                                published: Sequence[str],
                                target_tip: str) -> list[str]:
    """Finished folders this batch did not cover, in the usual publishing order.

    A batch receipt describes the folders that were finished when it was
    written.  Anything finished afterwards is simply not part of it, and saying
    so is what keeps a zero exit code from reading as "everything is published".
    """
    try:
        graph = parse_folder_dependency_graph(assent_dir)
        candidates = {folder for folder in graph
                      if folder not in set(published)
                      and infer_folder_completion(assent_dir / folder).complete}
        order = order_folders_by_dependency(graph, candidates)
    except AssentError:
        return []
    outstanding: list[str] = []
    for folder in order:
        if _no_source_remains(main, folder):
            continue
        try:
            cfg = load_config(config_path, folder)
            _branch, tip, _worktree = _source_snapshot(
                main, folder, cfg.git_excludes)
        except AssentError:
            outstanding.append(folder)
            continue
        if not gitops.is_ancestor(main, tip, target_tip):
            outstanding.append(folder)
    return outstanding


def _release_batch(config_path: str, assent_dir: Path) -> int | None:
    """Publish a fresh batch receipt's whole chain; ``None`` means fall back.

    ``accept --all`` chooses its path here and nowhere else.  A missing receipt,
    and an expired one, both return ``None`` after saying so, which hands the run
    to the unchanged per-folder path; only an unreadable receipt is a refusal,
    because malformed evidence is a broken state rather than permission to
    ignore it.
    """
    path = verification.batch_receipt_path(assent_dir)
    if not path.exists():
        print("accept --all: no batch verification receipt "
              "(`assent verify --batch` writes one).")
        return None
    try:
        with hold_integration_lock(assent_dir):
            return _release_batch_locked(config_path, assent_dir, path)
    except LockBusy as e:
        print(f"accept --all: refused ({e})")
        return 1
    except AssentError as e:
        print(f"accept --all: failed ({e})")
        return 1


def _release_batch_locked(config_path: str, assent_dir: Path,
                          path: Path) -> int | None:
    """Gate, replay, and publish one batch with the integration lock held."""
    main = gitops.main_worktree(assent_dir.parent)
    try:
        receipt = verification.read_batch_receipt(path, main)
    except AssentError as e:
        print(f"accept --all: refused, the batch verification receipt is "
              f"unusable ({e}). It is derived evidence: delete {path} and run "
              "`assent verify --batch` again")
        return 1
    try:
        # The batch's own order is fixed in the receipt, but a plan whose folder
        # graph stopped parsing is refused on both paths rather than published by
        # whichever one happens not to read it.
        parse_folder_dependency_graph(assent_dir)
    except AssentError as e:
        print(f"accept --all: refused, folder dependency graph is invalid ({e})")
        return 1
    configs = {folder: load_config(config_path, folder)
               for folder in receipt.folders}
    cfg = configs[receipt.folders[0]]

    if receipt.status != "PASSED":
        reasons: tuple[str, ...] = (f"its status is {receipt.status}",)
    else:
        try:
            reasons = verification.batch_receipt_staleness(cfg, receipt)
        except AssentError as e:
            reasons = (f"its freshness could not be proven: {e}",)
    if reasons:
        print("accept --all: the batch verification receipt has expired ("
              + "; ".join(reasons) + "); deleted, because a receipt is derived "
              "and disposable. Run `assent verify --batch` to build a new one.")
        verification.invalidate_batch_receipt(assent_dir)
        return None

    print(_BATCH_BANNER)
    print(f"  batch receipt covers: {', '.join(receipt.folders)}")

    with ExitStack() as locks:
        # The integration lock is already held; taking every batched folder's
        # own lock next keeps the fixed integration-then-folder order, and a
        # folder that is currently running refuses the whole release.
        for folder in receipt.folders:
            locks.enter_context(hold_lock(configs[folder].tasks_dir, folder))

        unfinished: list[str] = []
        for folder in receipt.folders:
            completion = infer_folder_completion(assent_dir / folder)
            if not completion.complete:
                unfinished.append(f"{folder} ({completion.reason})")
        if unfinished:
            print("accept --all: refused, batched folder(s) are no longer "
                  f"finished: {', '.join(unfinished)}. The target was not "
                  "changed; run `assent verify --batch` again")
            return 1

        sources, drift = _batch_source_identities(main, receipt, configs)
        if drift is not None:
            print(f"accept --all: refused, {drift}. The target was not changed; "
                  "run `assent verify --batch` again")
            return 1

        target_branch = gitops.require_current_branch(main)
        target_before = gitops.commit_of(main, "HEAD")
        problem = _batch_prerequisite_problem(
            main, assent_dir, configs, sources, target_before)
        if problem is not None:
            print(f"accept --all: refused, {problem}. The target was not "
                  "changed; accept the prerequisite first, or rebuild the batch "
                  "with `assent verify --batch` once it is finished")
            return 1

        problem = _batch_gate_problem(
            main, configs, sources, target_branch, target_before,
            receipt.verify_script_sha256)
        if problem is not None:
            print(f"accept --all: refused, {problem}. The target was not "
                  "changed and every source was kept")
            return 1

        replay = _BatchReplay()
        integration: Path | None = None
        integration_branch: str | None = None
        body_completed = False
        try:
            with gitops.temporary_integration_worktree(
                    main, "batch", target_before) as (candidate, candidate_branch):
                integration = candidate
                integration_branch = candidate_branch
                _replay_batch_chain(
                    candidate, target_branch, receipt, sources, replay)
                if replay.chain_head:
                    # The chain is proven; only now, and only once, does the
                    # target move -- there is no per-folder ref update that
                    # could leave half a batch published.
                    replay.problem = _batch_gate_problem(
                        main, configs, sources, target_branch, target_before,
                        receipt.verify_script_sha256) or ""
                    if not replay.problem:
                        gitops.fast_forward(main, replay.chain_head)
                        replay.published = True
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
            if not replay.published and not replay.problem \
                    and not replay.conflict_folder:
                return 1

        if replay.conflict_folder:
            print(f"accept --all: refused, replaying {replay.conflict_folder} "
                  f"into {target_branch} conflicts even though the batch "
                  "verified cleanly; the target was not changed. Conflicting "
                  "file(s):")
            for conflicting in replay.conflicts:
                print(f"  - {conflicting}")
            print("Run `assent verify --batch` to rebuild the batch candidate")
            return 1
        if replay.problem:
            print(f"accept --all: refused, {replay.problem}. The target was not "
                  "changed and every source was kept; run `assent verify "
                  "--batch` to rebuild the batch candidate")
            return 1

        consume_error = ""
        try:
            verification.invalidate_batch_receipt(assent_dir)
        except AssentError as e:
            consume_error = str(e)

        target_after = gitops.commit_of(main, "HEAD")
        print(f"accept --all: batch release done, {len(replay.merges)} folder(s) "
              "published by one ref update without running verification.")
        print(f"  target branch:   {target_branch}")
        print(f"  target before:   {target_before}")
        print(f"  target after:    {target_after}")
        print(f"  verified tree:   {receipt.final_tree}")
        print(f"  verifier digest: {receipt.verify_script_sha256}")
        print("  audit merges (one per folder, same shape and message as a "
              "single-folder accept):")
        for source, (folder, commit) in zip(sources, replay.merges):
            print(f"    {folder}: source {source.tip} -> merge {commit} "
                  f"(tree {source.step_tree})")
        print(f"  accepted:  {', '.join(receipt.folders)}")
        if consume_error:
            print(f"warning: the batch was published but its receipt could not "
                  f"be deleted ({consume_error}); delete {path} by hand. It "
                  "describes published work, so it can no longer authorize a "
                  "release.")
        else:
            print("  batch receipt consumed; rerunning `assent accept --all` is "
                  "a no-op for the folders it published.")
        outstanding = _unbatched_finished_folders(
            config_path, assent_dir, main, receipt.folders, target_after)
        if outstanding:
            print(f"  not covered by this batch: {', '.join(outstanding)} "
                  "(finished after it was verified). Run `assent accept --all` "
                  "again to publish them.")
        print("  source branch/worktree kept for every published folder; retain "
              "each while a dependent may still need its source evidence. "
              "`assent clean FOLDER` makes the final safety decision.")
        print("  The integration lock coordinates Assent commands only; do not "
              "run concurrent external Git writes during acceptance.")
        return 0


def accept_all(config_path: str, assent_dir: Path) -> int:
    """Publish one verified batch, or verify-then-accept every folder serially.

    ``--all`` picks its own path and always says which one it took: a fresh
    PASSED batch receipt is released as one transaction (``_release_batch``),
    and anything else -- no receipt, or an expired one -- runs the per-folder
    path below, whose behavior is unchanged.

    Per-folder path: verify-then-accept every finished work folder, serially,
    fail-closed.

    Selection and ordering reuse ``folderdeps`` exactly as ``run --all``
    does. Each folder refreshes its verification receipt only when stale
    (``verify_folder_if_needed``, the same unattended full verification as
    ``assent verify FOLDER``), then reuses ``accept_folder`` unchanged. A
    folder already published by a prior run skips straight to
    ``accept_folder``'s own idempotent path (see ``_already_integrated``).
    A finished folder whose source branch and worktree have both already
    been cleaned away (``_no_source_remains``) is skipped instead of run
    through verify/accept, since there is nothing left to verify; this
    skip does not count as a chain failure. Only ``--all`` skips this way --
    a directly named ``accept FOLDER`` still fails closed on a missing
    source, since a directly named target must never be silently skipped.
    The first real failure stops the remaining chain; folders already
    published stay published.
    """
    assent_dir = Path(assent_dir)
    released = _release_batch(config_path, assent_dir)
    if released is not None:
        return released
    print(_PER_FOLDER_BANNER)

    try:
        graph = parse_folder_dependency_graph(assent_dir)
    except AssentError as e:
        print(f"accept --all: refused, folder dependency graph is invalid ({e})")
        return 1
    if not graph:
        print("accept --all: no work folder with a task file found.")
        return 0

    finished: set[str] = set()
    for folder in graph:
        completion = infer_folder_completion(assent_dir / folder)
        if completion.complete:
            finished.add(folder)
        else:
            print(f"accept --all: skip {folder} (not finished: {completion.reason})")
    if not finished:
        print("accept --all: no finished work folder to accept.")
        return 0

    try:
        order = order_folders_by_dependency(graph, finished)
    except AssentError as e:
        print(f"accept --all: refused, {e}")
        return 1

    accepted: list[str] = []
    skipped: list[str] = []
    failure: tuple[str, str] | None = None
    processed = 0
    for folder in order:
        processed += 1
        try:
            cfg = load_config(config_path, folder)
        except AssentError as e:
            failure = (folder, f"config error: {e}")
            break
        main = gitops.main_worktree(cfg.root)
        if _no_source_remains(main, folder):
            print(f"accept --all: skip {folder} (no source branch remains; "
                  "already integrated and cleaned)")
            skipped.append(folder)
            continue
        if (not _already_integrated(cfg)
                and verification.verify_folder_if_needed(cfg) != 0):
            failure = (folder, "verification refused or failed")
            break
        if accept_folder(cfg) != 0:
            failure = (folder, "accept refused or failed")
            break
        accepted.append(folder)

    remaining = order[processed:]
    print("accept --all: summary")
    print(f"  accepted:  {', '.join(accepted) if accepted else '(none)'}")
    if skipped:
        print(f"  skipped:   {', '.join(skipped)} "
              "(no source remains; already integrated and cleaned)")
    if failure is not None:
        folder, reason = failure
        print(f"  failed:    {folder} ({reason})")
    print(f"  remaining: {', '.join(remaining) if remaining else '(none)'}")
    return 1 if failure is not None else 0
