"""``assent verify --batch``: one full verification for several folders.

This module decides which folders enter one candidate and in what order, builds
and verifies that candidate, offers the single conflict-skip question, localizes
a failure to the folder that causes it, and writes the resulting evidence.  The
evidence itself -- its schema, bytes, and freshness rules -- belongs to
``assent.batch_receipt``, which this module uses and never reaches into.
"""
from __future__ import annotations

import contextlib
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError, gitops
from assent.batch_receipt import (BATCH_RECEIPT_VERSION, BatchSource,
                                  BatchVerificationReceipt, batch_receipt_path,
                                  read_batch_receipt, write_batch_receipt)
from assent.config import Config, load_config
from assent.folderdeps import (infer_folder_completion, live_upstreams,
                               order_folders_by_dependency,
                               parse_folder_dependency_graph)
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.verification_common import (VERIFY_COMMAND, candidate_tree,
                                        invalidate_receipt, merge_chain,
                                        provisioned_candidate_links,
                                        run_full_verifier, sha256_file,
                                        source_snapshot, summary,
                                        union_worktree_links)


@dataclass(frozen=True)
class BatchSelection:
    """Which folders enter one batch candidate, and why the others do not."""

    sources: tuple[tuple[str, str], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def folders(self) -> tuple[str, ...]:
        return tuple(folder for folder, _tip in self.sources)


def _new_batch_receipt(*, status: str, target_tip: str,
                       sources: Sequence[tuple[str, str]],
                       step_trees: Sequence[str], digest: str, exit_code: int,
                       failure_summary: str = "") -> BatchVerificationReceipt:
    entries = tuple(
        BatchSource(folder, source_tip, step_tree)
        for (folder, source_tip), step_tree in zip(sources, step_trees))
    return BatchVerificationReceipt(
        version=BATCH_RECEIPT_VERSION,
        status=status,
        target_tip=target_tip,
        sources=entries,
        final_tree=step_trees[-1],
        verify_script_sha256=digest,
        verify_command=VERIFY_COMMAND,
        exit_code=exit_code,
        completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        failure_summary=summary(failure_summary),
    )


def select_batch_folders(config_path: str, assent_dir: Path, main: Path,
                         target_tip: str) -> tuple[BatchSelection,
                                                   dict[str, Config]]:
    """Pick every finished folder that still has work to publish, in merge order.

    Selection reuses the folder dependency graph and the same on-the-spot
    completion inference the scheduler uses, so a batch merges folders in
    exactly the order ``accept --all`` would publish them.  Two situations are a
    skip rather than a failure, matching ``accept --all``:

    * a folder that is not finished has nothing to verify yet;
    * a finished folder whose source branch and worktree are both gone was
      already integrated and cleaned, so there is nothing left to merge.

    A source tip already reachable from the target is likewise skipped: merging
    it would be a Git no-op, not a step a release could reproduce.  Any other
    unresolved source identity (detached, ambiguous, dirty) fails the whole
    batch instead of being silently dropped from the candidate.
    """
    graph = parse_folder_dependency_graph(assent_dir)
    finished: set[str] = set()
    skipped: list[tuple[str, str]] = []
    for folder in graph:
        completion = infer_folder_completion(assent_dir / folder)
        if completion.complete:
            finished.add(folder)
        else:
            skipped.append((folder, f"not finished: {completion.reason}"))

    configs: dict[str, Config] = {}
    sources: list[tuple[str, str]] = []
    for folder in order_folders_by_dependency(graph, finished):
        cfg = load_config(config_path, folder)
        if (gitops.folder_worktree(main, folder) is None
                and not gitops.folder_branches(main, folder)):
            skipped.append((folder, "no source branch remains; already "
                                    "integrated and cleaned"))
            continue
        _branch, source_tip, _worktree = source_snapshot(cfg, main)
        if gitops.is_ancestor(main, source_tip, target_tip):
            skipped.append(
                (folder, f"current source {source_tip[:12]} is already "
                         "contained in the target"))
            continue
        configs[folder] = cfg
        sources.append((folder, source_tip))
    return BatchSelection(tuple(sources), tuple(skipped)), configs


def _explicit_source_snapshot(cfg: Config, main: Path) -> tuple[str, str,
                                                                  Path | None]:
    """Resolve one named batch source only when its branch identity is unique."""
    branch = gitops.unique_folder_branch(main, cfg.tasks_name)
    if branch is None:
        raise AssentError(
            f"folder {cfg.tasks_name} has no source branch or worktree")
    current, tip, worktree = source_snapshot(cfg, main)
    if current != branch:
        raise AssentError(
            f"folder {cfg.tasks_name} source branch changed while it was being "
            "resolved")
    return current, tip, worktree


def select_explicit_batch_folders(
        config_path: str, assent_dir: Path, main: Path, target_tip: str,
        folder_names: Sequence[str]) -> tuple[BatchSelection, dict[str, Config]]:
    """Resolve an exact named folder set without dynamic skips or omissions.

    Every selected folder must be finished and have one clean source that is
    not already in the target.  Direct live prerequisites are either earlier in
    this normalized set or independently proven to be in the target.
    """
    names = tuple(folder_names)
    if len(names) < 2:
        raise AssentError(
            "an explicit selected batch needs at least two folder names")
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise AssentError(
            "an explicit selected batch cannot contain duplicate folder names: "
            + ", ".join(duplicates))

    graph = parse_folder_dependency_graph(assent_dir)
    missing = sorted(set(names) - set(graph))
    if missing:
        raise AssentError(
            "selected batch folder(s) were not found: " + ", ".join(missing))
    selected = set(names)
    ordered = order_folders_by_dependency(graph, selected)
    configs: dict[str, Config] = {}
    sources: list[tuple[str, str]] = []
    for folder in ordered:
        completion = infer_folder_completion(assent_dir / folder)
        if not completion.complete:
            raise AssentError(
                f"selected batch folder {folder} is not finished: "
                f"{completion.reason}")
        cfg = load_config(config_path, folder)
        _branch, source_tip, _worktree = _explicit_source_snapshot(cfg, main)
        if gitops.is_ancestor(main, source_tip, target_tip):
            raise AssentError(
                f"selected batch folder {folder} is already accepted into the "
                f"target (source {source_tip[:12]} is contained in it)")
        configs[folder] = cfg
        sources.append((folder, source_tip))

    earlier: set[str] = set()
    for folder in ordered:
        dependencies = graph[folder]
        for dependency in live_upstreams(assent_dir, dependencies):
            if dependency in selected:
                if dependency not in earlier:
                    raise AssentError(
                        f"selected batch prerequisite {dependency} of {folder} "
                        "was not normalized earlier in the selected set")
                continue
            dependency_completion = infer_folder_completion(
                assent_dir / dependency)
            if not dependency_completion.complete:
                raise AssentError(
                    f"prerequisite {dependency} of selected folder {folder} is "
                    f"not finished: {dependency_completion.reason}")
            dependency_cfg = load_config(config_path, dependency)
            _branch, dependency_tip, _worktree = _explicit_source_snapshot(
                dependency_cfg, main)
            if not gitops.is_ancestor(main, dependency_tip, target_tip):
                raise AssentError(
                    f"prerequisite {dependency} of selected folder {folder} "
                    f"(source {dependency_tip[:12]}) is neither in the target "
                    "nor present earlier in the selected batch")
        earlier.add(folder)
    return BatchSelection(tuple(sources)), configs


def _batch_drift(configs: dict[str, Config], main: Path, excludes: Sequence[str],
                 target_branch: str, target_tip: str,
                 sources: Sequence[tuple[str, str]], script: Path,
                 digest: str) -> list[str]:
    """Re-observe every identity the batch receipt is about to certify."""
    changed: list[str] = []
    if gitops.current_branch(main) != target_branch:
        changed.append("target branch changed")
    elif gitops.commit_of(main, target_branch) != target_tip:
        changed.append("target tip changed")
    if not gitops.working_tree_status(main, excludes).is_clean:
        changed.append("target worktree became dirty")
    for folder, source_tip in sources:
        try:
            _branch, current, _worktree = source_snapshot(configs[folder], main)
        except AssentError as e:
            changed.append(f"source for {folder} changed: {e}")
        else:
            if current != source_tip:
                changed.append(f"source tip for {folder} changed")
    if sha256_file(script) != digest:
        changed.append("verification script changed")
    return changed


@dataclass(frozen=True)
class _PrefixRun:
    """One full verification of a prefix of the recorded merge chain."""

    passed: bool
    step_trees: tuple[str, ...]
    exit_code: int
    failure_summary: str


@dataclass(frozen=True)
class BatchBisectResult:
    """Which folder broke the batch, and what is still provably good.

    ``kept`` is the longest prefix that was verified and passed during the
    search, so ``kept_step_trees`` comes from a real full verification and never
    from a re-run: the last passing step of a prefix bisection is always the
    prefix immediately before the guilty folder.
    """

    guilty: str
    guilty_summary: str
    kept: tuple[tuple[str, str], ...] = ()
    kept_step_trees: tuple[str, ...] = ()


def _bisect_steps(count: int) -> int:
    """Worst-case full verifications needed to localize among ``count`` folders."""
    steps = 0
    span = count
    while span > 1:
        span = (span + 1) // 2
        steps += 1
    return steps


def _prefix_links(worktrees: Mapping[str, Path],
                  sources: Sequence[tuple[str, str]]):
    """Union the provisioned links of exactly the folders in one merge chain."""
    return union_worktree_links(
        [worktrees.get(folder) for folder, _tip in sources])


def _verify_prefix(main: Path, target_tip: str,
                   sources: Sequence[tuple[str, str]], script: Path,
                   worktrees: Mapping[str, Path]) -> _PrefixRun:
    """Build and fully verify one prefix of an already-mergeable batch chain.

    The whole chain merged cleanly before localization started, so truncating it
    repeats a subset of the same merges and cannot conflict.  A conflict here
    would mean the repository changed underneath the search, which fails closed
    rather than being recorded as a test failure.

    Each prefix gets the provisioned links of its own folders, not the whole
    batch's, so localization verifies exactly the candidate it is judging.
    """
    with gitops.temporary_integration_worktree(
            main, "batch", target_tip) as (candidate, _branch):
        chain = merge_chain(candidate, sources)
        if not chain.ok:
            raise AssentError(
                f"merging {chain.conflict_folder} conflicts while localizing a "
                "batch failure, although the full chain merged cleanly; the "
                "repository changed during verification")
        with provisioned_candidate_links(
                candidate, _prefix_links(worktrees, sources)):
            try:
                result = run_full_verifier(script, candidate)
            except OSError as e:
                return _PrefixRun(False, chain.step_trees, 1,
                                  f"Unable to start verification: {e}")
    if result.returncode == 0:
        return _PrefixRun(True, chain.step_trees, 0, "")
    return _PrefixRun(
        False, chain.step_trees, result.returncode,
        summary(result.stdout, result.stderr,
                f"Verification command failed: {VERIFY_COMMAND} "
                f"(exit code {result.returncode})"))


def bisect_batch_failure(main: Path, target_tip: str,
                         sources: Sequence[tuple[str, str]], script: Path,
                         failure_summary: str,
                         worktrees: Mapping[str, Path] | None = None
                         ) -> BatchBisectResult:
    """Localize a failed batch to the first folder whose merge turns it red.

    The caller has already proven that the full chain merges cleanly and that
    verifying all of it fails, so the search looks for the smallest failing
    prefix by bisection: at most ``ceil(log2(N))`` full verifications instead of
    the ``N`` a folder-by-folder walk would cost.  Batching exists to spend fewer
    full runs, so localizing it must be cheap too.

    ``worktrees`` maps each folder to its source worktree so every prefix run
    receives the provisioned links of the folders it actually merges.
    """
    worktrees = {} if worktrees is None else worktrees
    total = len(sources)
    steps = _bisect_steps(total)
    if steps:
        print(f"verify --batch: localizing the failure over {total} folders; "
              f"at most {steps} more full verification(s)")
    low, high = 1, total
    last_pass: _PrefixRun | None = None
    guilty_summary = failure_summary
    step = 0
    while low < high:
        middle = (low + high) // 2
        step += 1
        prefix = tuple(sources[:middle])
        print(f"verify --batch: localizing step {step}/{steps}: verifying "
              f"{middle} of {total} folders ("
              + ", ".join(folder for folder, _tip in prefix) + ")")
        run = _verify_prefix(main, target_tip, prefix, script, worktrees)
        if run.passed:
            last_pass = run
            low = middle + 1
        else:
            high = middle
            guilty_summary = run.failure_summary

    guilty = sources[high - 1][0]
    if high == 1:
        return BatchBisectResult(guilty, guilty_summary)
    if last_pass is None or len(last_pass.step_trees) != high - 1:
        raise AssentError(
            f"batch localization ended at {guilty} without a verified result "
            f"for the {high - 1} folder(s) ahead of it")
    return BatchBisectResult(
        guilty, guilty_summary, tuple(sources[:high - 1]), last_pass.step_trees)


def _dependent_folders(graph: dict, folder: str,
                       candidates: Sequence[str]) -> tuple[str, ...]:
    """Every candidate that transitively declares ``after`` on ``folder``."""
    tainted = {folder}
    growing = True
    while growing:
        growing = False
        for name in candidates:
            if name in tainted or name not in graph:
                continue
            if tainted.intersection(graph[name].after):
                tainted.add(name)
                growing = True
    return tuple(name for name in candidates if name in tainted and name != folder)


@dataclass(frozen=True)
class BatchConflict:
    """One queued folder whose own merge into the batch candidate conflicted."""

    folder: str
    conflicts: tuple[str, ...] = ()
    source_tip: str = ""


@dataclass(frozen=True)
class FilteredBatchChain:
    """The merge chain that survived, plus every folder it had to leave out.

    ``skipped_after`` pairs an excluded folder with the conflicting folder it is
    queued ``after``: it was never attempted, because verifying it without the
    upstream it was written against would certify a candidate nobody asked for.
    """

    sources: tuple[tuple[str, str], ...] = ()
    step_trees: tuple[str, ...] = ()
    conflicts: tuple[BatchConflict, ...] = ()
    skipped_after: tuple[tuple[str, str], ...] = ()

    @property
    def folders(self) -> tuple[str, ...]:
        """Merged folder names in dependency order."""
        return tuple(folder for folder, _tip in self.sources)

    @property
    def skipped(self) -> tuple[str, ...]:
        """Every excluded folder: the conflicting ones, then their downstream."""
        return (tuple(conflict.folder for conflict in self.conflicts)
                + tuple(folder for folder, _cause in self.skipped_after))


def _merge_chain_skipping_conflicts(
        candidate: Path, sources: Sequence[tuple[str, str]],
        graph: dict) -> FilteredBatchChain:
    """Merge every source that still fits, recording the ones that do not.

    This is the only merge path that may leave a queued folder out, and it only
    ever proposes that to a human.  ``gitops.merge_no_ff`` aborts a conflicting
    merge itself, so the candidate stays at the last clean step and every later
    independent folder is still attempted: one scan collects all the conflicts a
    human then decides about once, instead of stopping at the first one.
    """
    queued = [folder for folder, _tip in sources]
    excluded: dict[str, str] = {}
    merged: list[tuple[str, str]] = []
    step_trees: list[str] = []
    conflicts: list[BatchConflict] = []
    for folder, source_tip in sources:
        if folder in excluded:
            continue
        previous = gitops.commit_of(candidate, "HEAD")
        message = f"verify(batch/{folder}): temporary integration candidate"
        outcome = gitops.merge_no_ff(candidate, source_tip, message)
        if not outcome.ok:
            conflicts.append(
                BatchConflict(folder, tuple(outcome.conflicts), source_tip))
            for downstream in _dependent_folders(graph, folder, queued):
                excluded.setdefault(downstream, folder)
            continue
        if gitops.commit_parents(candidate, "HEAD") != (previous, source_tip):
            raise AssentError(
                f"merging {folder} did not produce the expected two-parent "
                "batch candidate")
        merged.append((folder, source_tip))
        step_trees.append(gitops.tree_of(candidate, "HEAD"))
    return FilteredBatchChain(
        tuple(merged), tuple(step_trees), tuple(conflicts),
        tuple((folder, excluded[folder])
              for folder in queued if folder in excluded))


def confirm_on_terminal(question: str) -> bool:
    """Ask one yes/no question on the terminal; only a clear yes is a yes.

    There is deliberately no retry loop.  The question is always "may I verify
    less than you asked for", so an unclear answer, a closed stdin, or an
    unattended caller must land on no rather than stall the run.
    """
    try:
        answer = input(question)
    except EOFError:
        return False
    return answer.strip().lower() in ("", "y", "yes")


def _conflicts_with_target_alone(main: Path, conflict: BatchConflict,
                                 target_tip: str) -> bool:
    """True when the folder's own source already conflicts with the target.

    A batch conflict has two very different causes, and only this second,
    single-folder merge tells them apart: a source that conflicts with the
    integration target itself, and a source that conflicts only with an earlier
    unaccepted peer in the same batch.  ``reconcile`` addresses exactly the
    first one, so the distinction decides which advice is honest.
    """
    _tree, outcome = candidate_tree(
        main, conflict.folder, target_tip, conflict.source_tip)
    return not outcome.ok


def _report_batch_conflicts(chain: FilteredBatchChain, main: Path,
                            target_tip: str) -> None:
    """State every conflicting folder, its paths, and its excluded downstream."""
    for conflict in chain.conflicts:
        print(f"verify --batch: merging {conflict.folder} into the batch "
              "candidate conflicts. Conflicting file(s):")
        for conflicting in conflict.conflicts:
            print(f"  - {conflicting}")
        if _conflicts_with_target_alone(main, conflict, target_tip):
            print(f"verify --batch: {conflict.folder} conflicts with the "
                  "integration target on its own. Run `assent reconcile "
                  f"{conflict.folder}` to resolve that source-versus-target "
                  "conflict in an isolated worktree.")
        else:
            print(f"verify --batch: {conflict.folder} merges into the "
                  "integration target cleanly on its own; it conflicts only "
                  "with an earlier not-yet-accepted source in this batch. "
                  "`assent reconcile` handles one folder against the target "
                  "and never merges speculative peers, so it cannot resolve "
                  "this; accept the earlier folder first and verify again, or "
                  "reopen the folder with `assent rework <FOLDER> <TASK>` or "
                  f"drop it with `assent reject {conflict.folder}`.")
    for folder, cause in chain.skipped_after:
        print(f"verify --batch: {folder} is queued after {cause}, so it is "
              "skipped with it rather than verified without it")
    print("verify --batch: a source conflict is a human decision; accepting "
          "the folders ahead of it does not resolve it.")


def _skip_question(chain: FilteredBatchChain) -> str:
    """The single question that decides between a smaller batch and no batch."""
    return ("Skip " + ", ".join(chain.skipped) + " and verify the remaining "
            f"{len(chain.sources)} folder(s) (" + ", ".join(chain.folders)
            + ")? [Y/n]: ")


def _report_localization(assent_dir: Path, folders: Sequence[str],
                         result: BatchBisectResult,
                         requested_folders: Sequence[str] | None = None) -> str:
    """Print the localization verdict and return the note stored in the receipt.

    The guilty folder keeps its status and its task files: it is still finished
    work awaiting a human decision, and this command only says which folder it
    is.  ``rework`` and ``reject`` remain the only ways to reopen it.
    """
    try:
        graph = parse_folder_dependency_graph(assent_dir)
    except AssentError:
        # Localization is a diagnostic; an unparsable graph must not hide the
        # folder that was just proven guilty.
        graph = {}
    downstream = _dependent_folders(graph, result.guilty, folders)
    print(f"verify --batch: localized the failure to {result.guilty}")
    for line in result.guilty_summary.splitlines():
        print(f"  {line}")
    ejected = f"{result.guilty} is out of this batch"
    if downstream:
        ejected = (f"{result.guilty} and its downstream ("
                   + ", ".join(downstream) + ") are out of this batch")
    print(f"verify --batch: {ejected}. Its status and task files were not "
          f"touched; decide with `assent rework {result.guilty}` or "
          f"`assent reject {result.guilty}`")
    note = (f"Batch localization: {result.guilty} is the first folder whose "
            f"merge fails the full verification; {ejected}.")
    if result.kept:
        print("verify --batch: reissuing a PASSED batch receipt for the "
              f"{len(result.kept)} folder(s) verified ahead of it: "
              + ", ".join(folder for folder, _tip in result.kept))
        if requested_folders is not None:
            print("verify --batch: this smaller PASSED prefix receipt does not "
                  "authorize acceptance of the originally requested full set: "
                  + ", ".join(requested_folders))
    else:
        print("verify --batch: no folder ahead of it remains, so the batch "
              "keeps a FAILED receipt and publishes nothing")
    return summary(note, result.guilty_summary)


def _verify_batch_locked(config_path: str, assent_dir: Path, bisect: bool,
                         confirm: Callable[[str], bool],
                         selected_folders: Sequence[str] | None = None) -> int:
    """Build, verify, and record one batch candidate with every lock held.

    ``selected_folders`` switches the inherited dynamic skip/confirm policy to
    exact selection: the names are validated before the candidate is built and
    any merge conflict refuses the whole request without asking to skip it.
    """
    exact = selected_folders is not None
    root = assent_dir.parent
    main = gitops.main_worktree(root)
    path = batch_receipt_path(assent_dir)
    # A malformed batch receipt is evidence of an unsafe state, not permission
    # to erase it; this mirrors the single-folder path.
    if path.exists():
        read_batch_receipt(path, main)

    target_branch = gitops.require_current_branch(main)
    target_tip = gitops.commit_of(main, target_branch)
    if exact:
        selection, configs = select_explicit_batch_folders(
            config_path, assent_dir, main, target_tip, selected_folders)
    else:
        selection, configs = select_batch_folders(
            config_path, assent_dir, main, target_tip)
    for folder, reason in selection.skipped:
        print(f"verify --batch: skip {folder} ({reason})")
    if not selection.sources:
        if exact:
            raise AssentError(
                "the explicit selected batch resolved to no source folders")
        print("verify --batch: no folder has anything left to verify; "
              "no receipt was written.")
        return 0

    with contextlib.ExitStack() as locks:
        # The repository integration lock is already held; taking every queued
        # folder's own lock next keeps the fixed integration-then-folder order
        # and refuses to verify a folder that is currently running.
        for folder in selection.folders:
            locks.enter_context(hold_lock(configs[folder].tasks_dir, folder))

        excludes = configs[selection.folders[0]].git_excludes
        if not gitops.working_tree_status(main, excludes).is_clean:
            raise AssentError(f"target worktree {main} is not clean")
        # The source worktrees are kept, not discarded: each one may provision
        # ignored root-level directory links the full verifier needs, and only
        # the folders that actually enter the candidate contribute theirs.
        source_worktrees: dict[str, Path] = {}
        for folder, source_tip in selection.sources:
            if exact:
                _branch, current, worktree = _explicit_source_snapshot(
                    configs[folder], main)
            else:
                _branch, current, worktree = source_snapshot(
                    configs[folder], main)
            if current != source_tip:
                raise AssentError(
                    f"source tip for {folder} changed while the batch locks "
                    "were being acquired")
            if worktree is not None:
                source_worktrees[folder] = worktree
        script = (assent_dir / "verify.py").resolve()
        if not script.is_file():
            raise AssentError(f"Verification script not found: {script}")
        digest = sha256_file(script)

        print("verify --batch: merging "
              f"{len(selection.sources)} folder(s) in dependency order: "
              + ", ".join(selection.folders))
        invalidate_receipt(path)

        graph = parse_folder_dependency_graph(assent_dir)
        result: subprocess.CompletedProcess[str] | None = None
        start_failure = ""
        localized = False
        batch_sources: tuple[tuple[str, str], ...]
        batch_step_trees: tuple[str, ...]
        batch_folders: tuple[str, ...]
        batch_skipped: tuple[str, ...] = ()
        refusal = ""
        with gitops.temporary_integration_worktree(
                main, "batch", target_tip) as (candidate, _branch):
            if exact:
                chain = merge_chain(candidate, selection.sources)
                if not chain.ok:
                    print("verify --batch: the exact selected set conflicts "
                          f"while merging {chain.conflict_folder}. "
                          "Conflicting file(s):")
                    for conflict in chain.conflicts:
                        print(f"  - {conflict}")
                    print("verify --batch: refused; an explicit selected set "
                          "cannot be reduced by skipping a conflict. The target "
                          "and every source were left unchanged and no receipt "
                          "was written")
                    return 1
                batch_sources = tuple(selection.sources)
                batch_step_trees = chain.step_trees
                batch_folders = selection.folders
                with provisioned_candidate_links(
                        candidate,
                        _prefix_links(source_worktrees, batch_sources)):
                    try:
                        result = run_full_verifier(script, candidate)
                    except OSError as e:
                        start_failure = f"Unable to start verification: {e}"
            else:
                filtered = _merge_chain_skipping_conflicts(
                    candidate, selection.sources, graph)
                if filtered.conflicts:
                    _report_batch_conflicts(filtered, main, target_tip)
                if filtered.conflicts and not filtered.sources:
                    # Nothing is left to offer, so there is no decision to ask for.
                    refusal = ("every queued folder conflicts, so no independent "
                               "subset remains to verify")
                elif filtered.conflicts and not confirm(_skip_question(filtered)):
                    refusal = "the skip was declined, so nothing was verified"
                else:
                    with provisioned_candidate_links(
                            candidate,
                            _prefix_links(source_worktrees, filtered.sources)):
                        try:
                            result = run_full_verifier(script, candidate)
                        except OSError as e:
                            start_failure = f"Unable to start verification: {e}"
                if refusal:
                    print(f"verify --batch: refused, {refusal}. The target and "
                          "every source were left unchanged and no receipt was "
                          "written")
                    return 1
                batch_sources = filtered.sources
                batch_step_trees = filtered.step_trees
                batch_folders = filtered.folders
                batch_skipped = filtered.skipped

        # A human skip or a localization may shrink what the dynamic receipt
        # certifies. Exact selection starts from the complete requested set and
        # only bisection may retain a smaller verified prefix.
        sources = batch_sources
        step_trees = batch_step_trees
        if start_failure:
            status, exit_code, failure_summary = "FAILED", 1, start_failure
        else:
            assert result is not None
            status = "PASSED" if result.returncode == 0 else "FAILED"
            exit_code = result.returncode
            failure_summary = "" if result.returncode == 0 else summary(
                result.stdout, result.stderr,
                f"Verification command failed: {VERIFY_COMMAND} "
                f"(exit code {result.returncode})")
            if status == "FAILED" and bisect:
                bisected = bisect_batch_failure(
                    main, target_tip, batch_sources, script, failure_summary,
                    source_worktrees)
                failure_summary = _report_localization(
                    assent_dir, batch_folders, bisected,
                    batch_folders if exact else None)
                localized = True
                if bisected.kept:
                    sources = bisected.kept
                    step_trees = bisected.kept_step_trees
                    status, exit_code = "PASSED", 0

        receipt = _new_batch_receipt(
            status=status, target_tip=target_tip, sources=sources,
            step_trees=step_trees, digest=digest, exit_code=exit_code,
            failure_summary=failure_summary)

        changed = _batch_drift(
            configs, main, excludes, target_branch, target_tip,
            batch_sources, script, digest)
        if changed:
            if localized:
                print("verify --batch: the repository changed while the batch "
                      "was being verified, so the localization above certifies "
                      "nothing and the receipt records the drift instead")
            receipt = _new_batch_receipt(
                status="FAILED", target_tip=target_tip, sources=batch_sources,
                step_trees=batch_step_trees, digest=digest,
                exit_code=1, failure_summary="; ".join(changed))

        write_batch_receipt(path, receipt, main)

    for source in receipt.sources:
        print(f"  {source.folder}: source {source.source_tip[:12]} "
              f"-> tree {source.step_tree}")
    if receipt.status == "PASSED":
        if not localized:
            print(f"verify --batch: passed ({receipt.final_tree})")
            if batch_skipped:
                print("verify --batch: verified " + ", ".join(batch_folders)
                      + "; skipped " + ", ".join(batch_skipped))
            return 0
        # The batch as requested did not pass, so the exit code stays nonzero
        # even though a smaller batch is now certified: a caller must not read
        # success for folders that were just proven, or suspected, broken.
        print(f"verify --batch: failed, but {len(receipt.sources)} folder(s) "
              f"kept a PASSED batch receipt ({receipt.final_tree}); "
              "`assent accept --all` publishes exactly those")
        return 1
    print(f"verify --batch: failed ({receipt.failure_summary.splitlines()[0]})")
    return 1


def verify_batch(config_path: str, assent_dir: str | Path, bisect: bool = True,
                 confirm: Callable[[str], bool] = confirm_on_terminal) -> int:
    """Verify every queued folder as one candidate; zero only for PASSED.

    An empty batch is success with no receipt: there is nothing to certify, so
    inventing a receipt would be inventing evidence.

    Sources that conflict with the candidate are the one place a human is asked
    a question: ``confirm`` decides once whether to verify the independent
    subset that remains, or nothing at all.  It is a parameter so a test can
    answer without a terminal; the CLI leaves it at the ``input``-based default.

    A failed batch is localized to the folder that breaks it, and the folders
    ahead of that one keep the PASSED receipt they were already proven to earn.
    ``bisect=False`` turns that off and simply records the failure.
    """
    assent_dir = Path(assent_dir)
    try:
        with hold_integration_lock(assent_dir):
            return _verify_batch_locked(
                config_path, assent_dir, bisect, confirm)
    except LockBusy as e:
        print(f"verify --batch: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify --batch: failed ({e})")
        return 1


def verify_selected_batch(config_path: str, assent_dir: str | Path,
                          folders: Sequence[str], bisect: bool = True) -> int:
    """Verify exactly the named folders as one dependency-ordered candidate.

    Unlike the dynamic batch path, a selected conflict is a refusal rather than
    an invitation to skip folders.  The requested set is never broadened or
    silently reduced, and a smaller PASSED prefix from bisection still returns
    nonzero because it does not certify the original request.
    """
    assent_dir = Path(assent_dir)
    try:
        with hold_integration_lock(assent_dir):
            return _verify_batch_locked(
                config_path, assent_dir, bisect, confirm_on_terminal, folders)
    except LockBusy as e:
        print(f"verify --batch: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify --batch: failed ({e})")
        return 1


# Keep both word orders discoverable for library callers while the CLI uses the
# explicit ``verify_selected_batch`` name.
verify_batch_selected = verify_selected_batch
