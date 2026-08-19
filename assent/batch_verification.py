"""``assent verify --batch``: one full verification for several plans.

This module decides which plans enter one candidate and in what order, builds
and verifies that candidate, offers the single conflict-skip question, localizes
a failure to the plan that causes it, and writes the resulting evidence.  The
evidence itself -- its schema, bytes, and freshness rules -- belongs to
``assent.batch_receipt``, which this module uses and never reaches into.
"""
from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError, gitops, shared_paths
from assent.batch_receipt import (BATCH_RECEIPT_VERSION, BatchSource,
                                  BatchVerificationReceipt, batch_receipt_path,
                                  batch_receipt_staleness, read_batch_receipt,
                                  write_batch_receipt)
from assent.config import Config, load_config
from assent.plandeps import (infer_plan_completion, live_upstreams,
                               order_plans_by_dependency,
                               parse_plan_dependency_graph)
from assent.init import recover_expanded_bridge_drift
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.verification_common import (VERIFY_COMMAND, candidate_tree,
                                        BatchCandidate, FullVerifyEvidence,
                                        ignored_input_diagnosis,
                                        invalidate_receipt, merge_chain,
                                        print_ignored_input_diagnosis,
                                        provisioned_candidate_links,
                                        run_full_verifier, sha256_file,
                                        source_snapshot, summary,
                                        union_worktree_links)


@dataclass(frozen=True)
class BatchSelection:
    """Which plans enter one batch candidate, and why the others do not."""

    sources: tuple[tuple[str, str], ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def plan_names(self) -> tuple[str, ...]:
        return tuple(plan_name for plan_name, _tip in self.sources)


def _new_batch_receipt(*, status: str, target_tip: str,
                       sources: Sequence[tuple[str, str]],
                       step_trees: Sequence[str], digest: str,
                       shared_inputs: str, exit_code: int,
                       failure_summary: str = "") -> BatchVerificationReceipt:
    entries = tuple(
        BatchSource(plan_name, source_tip, step_tree)
        for (plan_name, source_tip), step_tree in zip(sources, step_trees))
    return BatchVerificationReceipt(
        version=BATCH_RECEIPT_VERSION,
        status=status,
        target_tip=target_tip,
        sources=entries,
        final_tree=step_trees[-1],
        verify_script_sha256=digest,
        shared_inputs_sha256=shared_inputs,
        verify_command=VERIFY_COMMAND,
        exit_code=exit_code,
        completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        failure_summary=summary(failure_summary),
    )


def _batch_evidence(receipt: BatchVerificationReceipt, *,
                    reused: bool) -> FullVerifyEvidence:
    outcome = ("PASSED" if receipt.status == "PASSED" else
               "INFRASTRUCTURE_FAILED" if receipt.failure_summary.startswith(
                   ("Unable to start verification:",
                    "target branch changed", "target tip changed",
                    "target worktree became dirty", "source tip changed",
                    "source worktree became dirty", "source for ",
                    "verification script changed",
                    "a declared shared input changed",
                    "shared inputs became unreadable")) else
               "VERIFIER_FAILED")
    return FullVerifyEvidence(
        outcome, receipt.plan_names, receipt.target_tip,
        tuple(source.source_tip for source in receipt.sources),
        receipt.final_tree, receipt.verify_script_sha256,
        receipt.shared_inputs_sha256, receipt.exit_code,
        tuple(item for item in (receipt.failure_summary,) if item), reused)


def select_batch_plans(config_path: str, assent_dir: Path, main: Path,
                         target_tip: str) -> tuple[BatchSelection,
                                                   dict[str, Config]]:
    """Pick every finished plan that still has work to publish, in merge order.

    Selection reuses the plan dependency graph and the same on-the-spot
    completion inference the scheduler uses, so a batch merges plans in
    exactly the order ``accept --all`` would publish them.  Two situations are a
    skip rather than a failure, matching ``accept --all``:

    * a plan that is not finished has nothing to verify yet;
    * a finished plan whose source branch and worktree are both gone was
      already integrated and cleaned, so there is nothing left to merge.

    A source tip already reachable from the target is likewise skipped: merging
    it would be a Git no-op, not a step a release could reproduce.  Any other
    unresolved source identity (detached, ambiguous, dirty) fails the whole
    batch instead of being silently dropped from the candidate.
    """
    graph = parse_plan_dependency_graph(assent_dir)
    finished: set[str] = set()
    skipped: list[tuple[str, str]] = []
    for plan_name in graph:
        completion = infer_plan_completion(assent_dir / plan_name)
        if completion.complete:
            finished.add(plan_name)
        else:
            skipped.append((plan_name, f"not finished: {completion.reason}"))

    configs: dict[str, Config] = {}
    sources: list[tuple[str, str]] = []
    for plan_name in order_plans_by_dependency(graph, finished):
        cfg = load_config(config_path, plan_name)
        if (gitops.plan_worktree(main, plan_name) is None
                and not gitops.plan_branches(main, plan_name)):
            skipped.append((plan_name, "no source branch remains; already "
                                    "integrated and cleaned"))
            continue
        _branch, source_tip, _worktree = source_snapshot(cfg, main)
        if gitops.is_ancestor(main, source_tip, target_tip):
            skipped.append(
                (plan_name, f"current source {source_tip[:12]} is already "
                         "contained in the target"))
            continue
        configs[plan_name] = cfg
        sources.append((plan_name, source_tip))
    return BatchSelection(tuple(sources), tuple(skipped)), configs


def _explicit_source_snapshot(cfg: Config, main: Path) -> tuple[str, str,
                                                                  Path | None]:
    """Resolve one named batch source only when its branch identity is unique."""
    branch = gitops.unique_plan_branch(main, cfg.tasks_name)
    if branch is None:
        raise AssentError(
            f"plan {cfg.tasks_name} has no source branch or worktree")
    current, tip, worktree = source_snapshot(cfg, main)
    if current != branch:
        raise AssentError(
            f"plan {cfg.tasks_name} source branch changed while it was being "
            "resolved")
    return current, tip, worktree


def select_explicit_batch_plans(
        config_path: str, assent_dir: Path, main: Path, target_tip: str,
        plan_names: Sequence[str]) -> tuple[BatchSelection, dict[str, Config]]:
    """Resolve an exact named plan set without dynamic skips or omissions.

    Every selected plan must be finished and have one clean source that is
    not already in the target.  Direct live prerequisites are either earlier in
    this normalized set or independently proven to be in the target.
    """
    names = tuple(plan_names)
    if len(names) < 2:
        raise AssentError(
            "an explicit selected batch needs at least two plan names")
    if len(set(names)) != len(names):
        duplicates = sorted({name for name in names if names.count(name) > 1})
        raise AssentError(
            "an explicit selected batch cannot contain duplicate plan names: "
            + ", ".join(duplicates))

    graph = parse_plan_dependency_graph(assent_dir)
    missing = sorted(set(names) - set(graph))
    if missing:
        raise AssentError(
            "selected batch plan(s) were not found: " + ", ".join(missing))
    selected = set(names)
    ordered = order_plans_by_dependency(graph, selected)
    configs: dict[str, Config] = {}
    sources: list[tuple[str, str]] = []
    for plan_name in ordered:
        completion = infer_plan_completion(assent_dir / plan_name)
        if not completion.complete:
            raise AssentError(
                f"selected batch plan {plan_name} is not finished: "
                f"{completion.reason}")
        cfg = load_config(config_path, plan_name)
        _branch, source_tip, _worktree = _explicit_source_snapshot(cfg, main)
        if gitops.is_ancestor(main, source_tip, target_tip):
            raise AssentError(
                f"selected batch plan {plan_name} is already accepted into the "
                f"target (source {source_tip[:12]} is contained in it)")
        configs[plan_name] = cfg
        sources.append((plan_name, source_tip))

    earlier: set[str] = set()
    for plan_name in ordered:
        dependencies = graph[plan_name]
        for dependency in live_upstreams(assent_dir, dependencies):
            if dependency in selected:
                if dependency not in earlier:
                    raise AssentError(
                        f"selected batch prerequisite {dependency} of {plan_name} "
                        "was not normalized earlier in the selected set")
                continue
            dependency_completion = infer_plan_completion(
                assent_dir / dependency)
            if not dependency_completion.complete:
                raise AssentError(
                    f"prerequisite {dependency} of selected plan {plan_name} is "
                    f"not finished: {dependency_completion.reason}")
            dependency_cfg = load_config(config_path, dependency)
            _branch, dependency_tip, _worktree = _explicit_source_snapshot(
                dependency_cfg, main)
            if not gitops.is_ancestor(main, dependency_tip, target_tip):
                raise AssentError(
                    f"prerequisite {dependency} of selected plan {plan_name} "
                    f"(source {dependency_tip[:12]}) is neither in the target "
                    "nor present earlier in the selected batch")
        earlier.add(plan_name)
    return BatchSelection(tuple(sources)), configs


def _shared_digest(main: Path, contracts: Mapping[str, shared_paths.Contract],
                   sources: Sequence[tuple[str, str]]) -> str:
    """The shared-input digest for exactly the plans a receipt will record.

    A batch may shrink -- a declined conflict, a skip, a bisected prefix -- so
    the digest is taken over the recorded merge order rather than over whatever
    was classified at the start; the receipt must describe the set it certifies.
    """
    return shared_paths.shared_inputs_digest(
        main, [(plan_name, contracts[plan_name])
               for plan_name, _tip in sources if plan_name in contracts])


def _current_shared_digest(
        main: Path, sources: Sequence[tuple[str, str]],
        source_worktrees: Mapping[str, Path]) -> str:
    """Reclassify one batch source set without repairing any worktree links."""
    manifest = shared_paths.read_manifest(main)
    contracts: list[tuple[str, shared_paths.Contract]] = []
    for plan_name, _tip in sources:
        contract = shared_paths.classify(
            main, source_worktrees.get(plan_name) or main, manifest=manifest)
        if not contract.settled:
            raise AssentError(
                f"the shared-path contract for {plan_name} is {contract.state}; "
                "the batch's shared-input evidence can no longer be reproduced")
        shared_paths.require_directory_link_agreement(
            main, source_worktrees.get(plan_name) or main, contract,
            plan_name=plan_name)
        contracts.append((plan_name, contract))
    return shared_paths.shared_inputs_digest(main, contracts)


def _batch_drift(configs: dict[str, Config], main: Path, excludes: Sequence[str],
                 target_branch: str, target_tip: str,
                 sources: Sequence[tuple[str, str]], script: Path,
                 digest: str, contracts: Mapping[str, shared_paths.Contract],
                 shared_sources: Sequence[tuple[str, str]],
                 shared_inputs: str,
                 source_worktrees: Mapping[str, Path]) -> list[str]:
    """Re-observe every identity the batch receipt is about to certify."""
    changed: list[str] = []
    if gitops.current_branch(main) != target_branch:
        changed.append("target branch changed")
    elif gitops.commit_of(main, target_branch) != target_tip:
        changed.append("target tip changed")
    if not gitops.working_tree_status(main, excludes).is_clean:
        changed.append("target worktree became dirty")
    for plan_name, source_tip in sources:
        try:
            _branch, current, _worktree = source_snapshot(configs[plan_name], main)
        except AssentError as e:
            changed.append(f"source for {plan_name} changed: {e}")
        else:
            if current != source_tip:
                changed.append(f"source tip for {plan_name} changed")
    if sha256_file(script) != digest:
        changed.append("verification script changed")
    # Snapshotted again after the verifier: a declared shared target whose
    # content moved during the run turns an apparent pass into a failure, so no
    # PASSED batch receipt can describe inputs the verifier never saw.
    try:
        if _current_shared_digest(
                main, shared_sources, source_worktrees) != shared_inputs:
            changed.append(
                "a declared shared input changed while the full verifier was "
                "running, so the run certifies nothing")
    except AssentError as e:
        changed.append(f"shared inputs became unreadable: {e}")
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
    """Which plan broke the batch, and what is still provably good.

    ``kept`` is the longest prefix that was verified and passed during the
    search, so ``kept_step_trees`` comes from a real full verification and never
    from a re-run: the last passing step of a prefix bisection is always the
    prefix immediately before the guilty plan.
    """

    guilty: str
    guilty_summary: str
    kept: tuple[tuple[str, str], ...] = ()
    kept_step_trees: tuple[str, ...] = ()


def _bisect_steps(count: int) -> int:
    """Worst-case full verifications needed to localize among ``count`` plans."""
    steps = 0
    span = count
    while span > 1:
        span = (span + 1) // 2
        steps += 1
    return steps


def _prefix_links(worktrees: Mapping[str, Path],
                  sources: Sequence[tuple[str, str]]):
    """Union the provisioned links of exactly the plans in one merge chain."""
    return union_worktree_links(
        [worktrees.get(plan_name) for plan_name, _tip in sources])


def _failure_summary(result: subprocess.CompletedProcess[str],
                     worktrees: Mapping[str, Path],
                     sources: Sequence[tuple[str, str]]) -> str:
    """Summarize one failed batch run, with the ignored-input hint appended.

    Every batch entry point -- exact selection, dynamic discovery, and each
    localization prefix -- builds its failure evidence here, so the actionable
    fact is stored the same way whichever of them ran the verifier.
    """
    return summary(
        result.stdout, result.stderr,
        f"Verification command failed: {VERIFY_COMMAND} "
        f"(exit code {result.returncode})",
        ignored_input_diagnosis(
            f"{result.stdout}\n{result.stderr}",
            [worktrees.get(plan_name) for plan_name, _tip in sources]))


def _verify_prefix(main: Path, target_tip: str,
                   sources: Sequence[tuple[str, str]], script: Path,
                   worktrees: Mapping[str, Path]) -> _PrefixRun:
    """Build and fully verify one prefix of an already-mergeable batch chain.

    The whole chain merged cleanly before localization started, so truncating it
    repeats a subset of the same merges and cannot conflict.  A conflict here
    would mean the repository changed underneath the search, which fails closed
    rather than being recorded as a test failure.

    Each prefix gets the provisioned links of its own plans, not the whole
    batch's, so localization verifies exactly the candidate it is judging.
    """
    with gitops.temporary_integration_worktree(
            main, "batch", target_tip) as (candidate, _branch):
        chain = merge_chain(candidate, sources)
        if not chain.ok:
            raise AssentError(
                f"merging {chain.conflict_plan} conflicts while localizing a "
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
    return _PrefixRun(False, chain.step_trees, result.returncode,
                      _failure_summary(result, worktrees, sources))


def bisect_batch_failure(main: Path, target_tip: str,
                         sources: Sequence[tuple[str, str]], script: Path,
                         failure_summary: str,
                         worktrees: Mapping[str, Path] | None = None,
                         label: str = "verify --batch"
                         ) -> BatchBisectResult:
    """Localize a failed batch to the first plan whose merge turns it red.

    The caller has already proven that the full chain merges cleanly and that
    verifying all of it fails, so the search looks for the smallest failing
    prefix by bisection: at most ``ceil(log2(N))`` full verifications instead of
    the ``N`` a plan-by-plan walk would cost.  Batching exists to spend fewer
    full runs, so localizing it must be cheap too.

    ``worktrees`` maps each plan to its source worktree so every prefix run
    receives the provisioned links of the plans it actually merges.
    """
    worktrees = {} if worktrees is None else worktrees
    total = len(sources)
    steps = _bisect_steps(total)
    if steps:
        print(f"{label}: localizing the failure over {total} plans; "
              f"at most {steps} more full verification(s)")
    low, high = 1, total
    last_pass: _PrefixRun | None = None
    guilty_summary = failure_summary
    step = 0
    while low < high:
        middle = (low + high) // 2
        step += 1
        prefix = tuple(sources[:middle])
        print(f"{label}: localizing step {step}/{steps}: verifying "
              f"{middle} of {total} plans ("
              + ", ".join(plan_name for plan_name, _tip in prefix) + ")")
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
            f"for the {high - 1} plan(s) ahead of it")
    return BatchBisectResult(
        guilty, guilty_summary, tuple(sources[:high - 1]), last_pass.step_trees)


def _dependent_plans(graph: dict, plan_name: str,
                       candidates: Sequence[str]) -> tuple[str, ...]:
    """Every candidate that transitively declares ``after`` on ``plan_name``."""
    tainted = {plan_name}
    growing = True
    while growing:
        growing = False
        for name in candidates:
            if name in tainted or name not in graph:
                continue
            if tainted.intersection(graph[name].after):
                tainted.add(name)
                growing = True
    return tuple(name for name in candidates if name in tainted and name != plan_name)


@dataclass(frozen=True)
class BatchConflict:
    """One queued plan whose own merge into the batch candidate conflicted."""

    plan: str
    conflicts: tuple[str, ...] = ()
    source_tip: str = ""


@dataclass(frozen=True)
class SelectionCandidateConflict:
    """One source-bound conflict found before an exact-selection verifier runs."""

    plan: str
    paths: tuple[str, ...]
    source_tip: str
    target_tip: str
    prefix_sources: tuple[tuple[str, str], ...]
    prefix_tree: str
    dependent_exclusions: tuple[str, ...]
    kind: str


@dataclass(frozen=True)
class SelectionConflictEvidence(FullVerifyEvidence):
    """Complete candidate-conflict wave carried beside ordinary action evidence."""

    conflicts: tuple[SelectionCandidateConflict, ...] = ()


_SELECTION_CONFLICT_PREFIX = "SELECTION_CONFLICT "


def selection_conflict_line(conflict: SelectionCandidateConflict) -> str:
    """Encode one typed conflict for the durable selection workflow cursor."""
    return _SELECTION_CONFLICT_PREFIX + json.dumps({
        "plan": conflict.plan,
        "paths": list(conflict.paths),
        "source_tip": conflict.source_tip,
        "target_tip": conflict.target_tip,
        "prefix_sources": [list(item) for item in conflict.prefix_sources],
        "prefix_tree": conflict.prefix_tree,
        "dependent_exclusions": list(conflict.dependent_exclusions),
        "kind": conflict.kind,
    }, separators=(",", ":"), sort_keys=True)


def selection_conflicts_from_evidence(
        evidence: Sequence[str]) -> tuple[SelectionCandidateConflict, ...]:
    """Decode and validate the conflict wave persisted by a selection action."""
    conflicts: list[SelectionCandidateConflict] = []
    for line in evidence:
        if not line.startswith(_SELECTION_CONFLICT_PREFIX):
            continue
        try:
            value = json.loads(line[len(_SELECTION_CONFLICT_PREFIX):])
            prefix = value["prefix_sources"]
            conflict = SelectionCandidateConflict(
                value["plan"], tuple(value["paths"]), value["source_tip"],
                value["target_tip"],
                tuple((item[0], item[1]) for item in prefix),
                value["prefix_tree"], tuple(value["dependent_exclusions"]),
                value["kind"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError,
                IndexError) as error:
            raise AssentError(
                "selection candidate conflict evidence is malformed") from error
        if (not conflict.plan or not conflict.paths
                or not all(isinstance(item, str) and item
                           for item in conflict.paths)
                or not conflict.source_tip or not conflict.target_tip
                or not conflict.prefix_tree
                or conflict.kind not in {"target_alone", "peer_only"}
                or not all(isinstance(plan_name, str) and plan_name
                           and isinstance(tip, str) and tip
                           for plan_name, tip in conflict.prefix_sources)
                or not all(isinstance(item, str) and item
                           for item in conflict.dependent_exclusions)):
            raise AssentError(
                "selection candidate conflict evidence has invalid values")
        conflicts.append(conflict)
    return tuple(conflicts)


@dataclass(frozen=True)
class FilteredBatchChain:
    """The merge chain that survived, plus every plan it had to leave out.

    ``skipped_after`` pairs an excluded plan with the conflicting plan it is
    queued ``after``: it was never attempted, because verifying it without the
    upstream it was written against would certify a candidate nobody asked for.
    """

    sources: tuple[tuple[str, str], ...] = ()
    step_trees: tuple[str, ...] = ()
    conflicts: tuple[BatchConflict, ...] = ()
    skipped_after: tuple[tuple[str, str], ...] = ()

    @property
    def plan_names(self) -> tuple[str, ...]:
        """Merged plan names in dependency order."""
        return tuple(plan_name for plan_name, _tip in self.sources)

    @property
    def skipped(self) -> tuple[str, ...]:
        """Every excluded plan: the conflicting ones, then their downstream."""
        return (tuple(conflict.plan for conflict in self.conflicts)
                + tuple(plan_name for plan_name, _cause in self.skipped_after))


@dataclass(frozen=True)
class _ReviewedSelectionChain:
    """One complete conflict scan, including every independent clean merge."""

    sources: tuple[tuple[str, str], ...]
    step_trees: tuple[str, ...]
    conflicts: tuple[SelectionCandidateConflict, ...]


def _merge_chain_collecting_selection_conflicts(
        candidate: Path, main: Path, target_tip: str,
        sources: Sequence[tuple[str, str]], graph: dict
        ) -> _ReviewedSelectionChain:
    """Collect one complete exact-selection conflict wave without verifying.

    A conflicting plan and every selected dependent are excluded from the
    remainder of this diagnostic build, while later independent sources are
    still attempted.  This is evidence collection only: the exact selection is
    neither shrunk nor certified, and the caller starts no verifier when any
    conflict was found.
    """
    queued = [plan_name for plan_name, _tip in sources]
    excluded: set[str] = set()
    merged: list[tuple[str, str]] = []
    step_trees: list[str] = []
    conflicts: list[SelectionCandidateConflict] = []
    for plan_name, source_tip in sources:
        if plan_name in excluded:
            continue
        prefix_sources = tuple(merged)
        prefix_tree = gitops.tree_of(candidate, "HEAD")
        previous = gitops.commit_of(candidate, "HEAD")
        outcome = gitops.merge_no_ff(
            candidate, source_tip,
            f"verify(batch/{plan_name}): temporary integration candidate")
        if not outcome.ok:
            dependents = _dependent_plans(graph, plan_name, queued)
            excluded.update(dependents)
            target_alone = _conflicts_with_target_alone(
                main, BatchConflict(plan_name, tuple(outcome.conflicts),
                                    source_tip), target_tip)
            conflicts.append(SelectionCandidateConflict(
                plan_name, tuple(outcome.conflicts), source_tip, target_tip,
                prefix_sources, prefix_tree, dependents,
                "target_alone" if target_alone else "peer_only"))
            continue
        if gitops.commit_parents(candidate, "HEAD") != (previous, source_tip):
            raise AssentError(
                f"merging {plan_name} did not produce the expected two-parent "
                "selection candidate")
        merged.append((plan_name, source_tip))
        step_trees.append(gitops.tree_of(candidate, "HEAD"))
    return _ReviewedSelectionChain(
        tuple(merged), tuple(step_trees), tuple(conflicts))


def _merge_chain_skipping_conflicts(
        candidate: Path, sources: Sequence[tuple[str, str]],
        graph: dict) -> FilteredBatchChain:
    """Merge every source that still fits, recording the ones that do not.

    This is the only merge path that may leave a queued plan out, and it only
    ever proposes that to a human.  ``gitops.merge_no_ff`` aborts a conflicting
    merge itself, so the candidate stays at the last clean step and every later
    independent plan is still attempted: one scan collects all the conflicts a
    human then decides about once, instead of stopping at the first one.
    """
    queued = [plan_name for plan_name, _tip in sources]
    excluded: dict[str, str] = {}
    merged: list[tuple[str, str]] = []
    step_trees: list[str] = []
    conflicts: list[BatchConflict] = []
    for plan_name, source_tip in sources:
        if plan_name in excluded:
            continue
        previous = gitops.commit_of(candidate, "HEAD")
        message = f"verify(batch/{plan_name}): temporary integration candidate"
        outcome = gitops.merge_no_ff(candidate, source_tip, message)
        if not outcome.ok:
            conflicts.append(
                BatchConflict(plan_name, tuple(outcome.conflicts), source_tip))
            for downstream in _dependent_plans(graph, plan_name, queued):
                excluded.setdefault(downstream, plan_name)
            continue
        if gitops.commit_parents(candidate, "HEAD") != (previous, source_tip):
            raise AssentError(
                f"merging {plan_name} did not produce the expected two-parent "
                "batch candidate")
        merged.append((plan_name, source_tip))
        step_trees.append(gitops.tree_of(candidate, "HEAD"))
    return FilteredBatchChain(
        tuple(merged), tuple(step_trees), tuple(conflicts),
        tuple((plan_name, excluded[plan_name])
              for plan_name in queued if plan_name in excluded))


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
    """True when the plan's own source already conflicts with the target.

    A batch conflict has two very different causes, and only this second,
    single-plan merge tells them apart: a source that conflicts with the
    integration target itself, and a source that conflicts only with an earlier
    unaccepted peer in the same batch.  ``reconcile`` addresses exactly the
    first one, so the distinction decides which advice is honest.
    """
    _tree, outcome = candidate_tree(
        main, conflict.plan, target_tip, conflict.source_tip)
    return not outcome.ok


def _report_batch_conflicts(chain: FilteredBatchChain, main: Path,
                            target_tip: str) -> None:
    """State every conflicting plan, its paths, and its excluded downstream."""
    for conflict in chain.conflicts:
        print(f"verify --batch: merging {conflict.plan} into the batch "
              "candidate conflicts. Conflicting file(s):")
        for conflicting in conflict.conflicts:
            print(f"  - {conflicting}")
        if _conflicts_with_target_alone(main, conflict, target_tip):
            print(f"verify --batch: {conflict.plan} conflicts with the "
                  "integration target on its own. Run `assent reconcile "
                  f"{conflict.plan}` to resolve that source-versus-target "
                  "conflict in an isolated worktree.")
        else:
            print(f"verify --batch: {conflict.plan} merges into the "
                  "integration target cleanly on its own; it conflicts only "
                  "with an earlier not-yet-accepted source in this batch. "
                  "`assent reconcile` handles one plan against the target "
                  "and never merges speculative peers, so it cannot resolve "
                  "this; verify and accept compatible work ahead first, then "
                  "run `assent reconcile <PLAN>` against the advanced target, "
                  "or "
                  "reopen the plan with `assent rework <PLAN> <TASK>` or "
                  f"drop it with `assent reject {conflict.plan}`.")
    for plan_name, cause in chain.skipped_after:
        print(f"verify --batch: {plan_name} is queued after {cause}, so it is "
              "skipped with it rather than verified without it")
    print("verify --batch: a source conflict is a human decision; accepting "
          "compatible work ahead advances the target but does not resolve the "
          "conflicting source.")


def _skip_question(chain: FilteredBatchChain) -> str:
    """The single question that decides between a smaller batch and no batch."""
    return ("Skip " + ", ".join(chain.skipped) + " and verify the remaining "
            f"{len(chain.sources)} plan(s) (" + ", ".join(chain.plan_names)
            + ")? [Y/n]: ")


def _report_localization(assent_dir: Path, plan_names: Sequence[str],
                         result: BatchBisectResult,
                         requested_plans: Sequence[str] | None = None,
                         label: str = "verify --batch") -> str:
    """Print the localization verdict and return the note stored in the receipt.

    The guilty plan keeps its status and its task files: it is still finished
    work awaiting a human decision, and this command only says which plan it
    is.  ``rework`` and ``reject`` remain the only ways to reopen it.
    """
    try:
        graph = parse_plan_dependency_graph(assent_dir)
    except AssentError:
        # Localization is a diagnostic; an unparsable graph must not hide the
        # plan that was just proven guilty.
        graph = {}
    downstream = _dependent_plans(graph, result.guilty, plan_names)
    print(f"{label}: localized the failure to {result.guilty}")
    for line in result.guilty_summary.splitlines():
        print(f"  {line}")
    ejected = f"{result.guilty} is out of this batch"
    if downstream:
        ejected = (f"{result.guilty} and its downstream ("
                   + ", ".join(downstream) + ") are out of this batch")
    print(f"{label}: {ejected}. Its status and task files were not "
          f"touched; a human must select the guilty task before running "
          f"`assent rework {result.guilty} <TASK>`, or can run "
          f"`assent reject {result.guilty}` for the whole-plan alternative")
    note = (f"Batch localization: {result.guilty} is the first plan whose "
            f"merge fails the full verification; {ejected}.")
    if result.kept:
        print(f"{label}: reissuing a PASSED batch receipt for the "
              f"{len(result.kept)} plan(s) verified ahead of it: "
              + ", ".join(plan_name for plan_name, _tip in result.kept))
        if requested_plans is not None:
            print(f"{label}: this smaller PASSED prefix receipt does not "
                  "authorize acceptance of the originally requested full set: "
                  + ", ".join(requested_plans))
    else:
        print(f"{label}: no plan ahead of it remains, so the batch "
              "keeps a FAILED receipt and publishes nothing")
    return summary(note, result.guilty_summary)


def _selected_prefix(sources: Sequence[tuple[str, str]],
                    conflict_plan: str) -> tuple[str, ...]:
    """Return the selected plans proven merge-compatible before a conflict."""
    for index, (plan_name, _tip) in enumerate(sources):
        if plan_name == conflict_plan:
            return tuple(name for name, _source_tip in sources[:index])
    return ()


def _report_selected_conflict(chain: BatchCandidate, main: Path,
                              target_tip: str,
                              sources: Sequence[tuple[str, str]]) -> None:
    """Report an exact conflict and give recovery that preserves its set."""
    plan_name = chain.conflict_plan
    print("verify selected: candidate construction encountered merge "
          "conflicts; the full verifier did not run.")
    print(f"verify selected: the exact selected set conflicts while merging "
          f"{plan_name} into the candidate. Conflicting file(s):")
    for conflict in chain.conflicts:
        print(f"  - {conflict}")
    print("verify selected: no receipt was written; all target/source refs "
          "(the integration target ref and every selected source ref) were left "
          "unchanged.")

    source_tip = dict(sources)[plan_name]
    if _conflicts_with_target_alone(
            main, BatchConflict(plan_name, chain.conflicts, source_tip), target_tip):
        print(f"verify selected: {plan_name} conflicts with the integration "
              "target on its own. Run `assent reconcile "
              f"{plan_name}` to resolve that source-versus-target conflict.")
        return

    prefix = _selected_prefix(sources, plan_name)
    if prefix:
        prefix_text = ", ".join(prefix)
        verify_command = "assent verify " + " ".join(prefix)
        accept_command = "assent accept " + " ".join(prefix)
        print(f"verify selected: {plan_name} merges into the integration target "
              "cleanly on its own; it conflicts only with the selected "
              "sources ahead of it.")
        print(f"verify selected: compatible selected prefix ahead of {plan_name}: "
              f"{prefix_text}. First run `{verify_command}`, then "
              f"`{accept_command}`. After the prefix advances the integration "
              f"target, run `assent reconcile {plan_name}` against that advanced "
              "target.")
    else:
        print(f"verify selected: {plan_name} is not independently conflicting "
              "with the integration target, but no compatible selected prefix "
              "was available; choose `assent rework <PLAN> <TASK>` or "
              f"`assent reject {plan_name}`.")
    print(f"verify selected: `assent rework <PLAN> <TASK>` and `assent reject "
          f"{plan_name}` remain explicit alternatives.")
    print("verify selected: the complete exact selection remains required; "
          "no prefix was accepted and no full verifier ran.")


def _report_selection_conflict_wave(
        conflicts: Sequence[SelectionCandidateConflict]) -> None:
    """Report the complete scheduler-owned conflict scan without human advice."""
    print("verify selected: candidate construction encountered merge "
          "conflicts; the full verifier did not run.")
    for conflict in conflicts:
        print(f"verify selected: {conflict.plan} has a "
              f"{conflict.kind.replace('_', '-')} conflict on:")
        for path in conflict.paths:
            print(f"  - {path}")
        if conflict.dependent_exclusions:
            print("  dependent exclusions: "
                  + ", ".join(conflict.dependent_exclusions))
    print("verify selected: the complete exact selection remains required; "
          "no prefix was accepted and no full verifier ran.")


def _verify_batch_locked(config_path: str, assent_dir: Path, bisect: bool,
                         confirm: Callable[[str], bool],
                         selected_plans: Sequence[str] | None = None, *,
                         action_results: list[FullVerifyEvidence] | None = None,
                         recheck: bool = False) -> int:
    """Build, verify, and record one batch candidate with every lock held.

    ``selected_plans`` switches the inherited dynamic skip/confirm policy to
    exact selection: the names are validated before the candidate is built and
    any merge conflict refuses the whole request without asking to skip it.
    """
    exact = selected_plans is not None
    label = "verify selected" if exact else "verify --batch"
    root = assent_dir.parent
    main = gitops.main_worktree(root)
    path = batch_receipt_path(assent_dir)
    # A malformed batch receipt is evidence of an unsafe state, not permission
    # to erase it; this mirrors the single-plan path.
    existing = read_batch_receipt(path, main) if path.exists() else None

    target_branch = gitops.require_current_branch(main)
    target_tip = gitops.commit_of(main, target_branch)
    if exact:
        selection, configs = select_explicit_batch_plans(
            config_path, assent_dir, main, target_tip, selected_plans)
    else:
        selection, configs = select_batch_plans(
            config_path, assent_dir, main, target_tip)
    for plan_name, reason in selection.skipped:
        print(f"{label}: skip {plan_name} ({reason})")
    if not selection.sources:
        if exact:
            raise AssentError(
                "the explicit selected batch resolved to no source plans")
        print("verify --batch: no plan has anything left to verify; "
              "no receipt was written.")
        return 0

    with contextlib.ExitStack() as locks:
        # The repository integration lock is already held; taking every queued
        # plan's own lock next keeps the fixed integration-then-plan order
        # and refuses to verify a plan that is currently running.
        for plan_name in selection.plan_names:
            locks.enter_context(hold_lock(configs[plan_name].tasks_dir, plan_name))

        excludes = configs[selection.plan_names[0]].git_excludes
        if recover_expanded_bridge_drift(main):
            print("Recovered an Assent-generated AGENTS.md bridge update in "
                  "the target worktree.")
        if not gitops.working_tree_status(main, excludes).is_clean:
            raise AssentError(f"target worktree {main} is not clean")
        # The source worktrees are kept, not discarded: each one may provision
        # ignored root-level directory links the full verifier needs, and only
        # the plans that actually enter the candidate contribute theirs.
        source_worktrees: dict[str, Path] = {}
        for plan_name, source_tip in selection.sources:
            if exact:
                _branch, current, worktree = _explicit_source_snapshot(
                    configs[plan_name], main)
            else:
                _branch, current, worktree = source_snapshot(
                    configs[plan_name], main)
            if current != source_tip:
                raise AssentError(
                    f"source tip for {plan_name} changed while the batch locks "
                    "were being acquired")
            if worktree is not None:
                source_worktrees[plan_name] = worktree
        # Every contributing live source is classified and its Assent-owned
        # links reconciled before a candidate exists, so a batch never depends
        # on an earlier `run` having left a junction behind and UNKNOWN or STALE
        # refuses here with the zero-AI review remedy.
        contracts = dict(shared_paths.prepare_sources(
            main, [(plan_name, source_worktrees.get(plan_name))
                   for plan_name in selection.plan_names]))
        shared_before = _shared_digest(main, contracts, selection.sources)
        script = (assent_dir / "verify.py").resolve()
        if not script.is_file():
            raise AssentError(f"Verification script not found: {script}")
        digest = sha256_file(script)

        if (exact and existing is not None
                and existing.plan_names == selection.plan_names
                and tuple(source.source_tip for source in existing.sources)
                == tuple(tip for _plan_name, tip in selection.sources)
                and existing.verify_script_sha256 == digest
                and existing.shared_inputs_sha256 == shared_before
                and not batch_receipt_staleness(
                    configs[selection.plan_names[0]], existing)
                and (existing.status == "PASSED" or not recheck)):
            print(f"{label}: existing {existing.status} receipt is fresh; "
                  "full suite skipped")
            if action_results is not None:
                action_results.append(_batch_evidence(existing, reused=True))
            return 0 if existing.status == "PASSED" else 1

        print(f"{label}: merging "
              f"{len(selection.sources)} plan(s) in dependency order: "
              + ", ".join(selection.plan_names))
        invalidate_receipt(path)

        graph = parse_plan_dependency_graph(assent_dir)
        result: subprocess.CompletedProcess[str] | None = None
        start_failure = ""
        localized = False
        batch_sources: tuple[tuple[str, str], ...]
        batch_step_trees: tuple[str, ...]
        batch_plans: tuple[str, ...]
        batch_skipped: tuple[str, ...] = ()
        refusal = ""
        with gitops.temporary_integration_worktree(
                main, "batch", target_tip) as (candidate, _branch):
            if exact:
                reviewed = (_merge_chain_collecting_selection_conflicts(
                    candidate, main, target_tip, selection.sources, graph)
                    if action_results is not None else None)
                if reviewed is not None and reviewed.conflicts:
                    _report_selection_conflict_wave(reviewed.conflicts)
                    encoded = tuple(
                        selection_conflict_line(item)
                        for item in reviewed.conflicts)
                    if len(encoded) > 15 or any(len(item) > 4096
                                               for item in encoded):
                        raise AssentError(
                            "selection conflict wave is too large for durable "
                            "workflow evidence")
                    action_results.append(SelectionConflictEvidence(
                        ("TARGET_CONFLICT" if all(
                            item.kind == "target_alone"
                            for item in reviewed.conflicts)
                         else "PEER_CONFLICT"),
                        selection.plan_names, target_tip,
                        tuple(tip for _plan_name, tip in selection.sources),
                        (reviewed.step_trees[-1] if reviewed.step_trees else
                         gitops.tree_of(main, target_tip)),
                        digest, shared_before, 1, encoded, False,
                        reviewed.conflicts))
                    return 1
                chain = (BatchCandidate(reviewed.step_trees)
                         if reviewed is not None
                         else merge_chain(candidate, selection.sources))
                if not chain.ok:
                    _report_selected_conflict(
                        chain, main, target_tip, selection.sources)
                    if action_results is not None:
                        source_tip = dict(selection.sources)[chain.conflict_plan]
                        target_conflict = _conflicts_with_target_alone(
                            main, BatchConflict(
                                chain.conflict_plan, chain.conflicts,
                                source_tip), target_tip)
                        action_results.append(FullVerifyEvidence(
                            "TARGET_CONFLICT" if target_conflict
                            else "PEER_CONFLICT",
                            selection.plan_names, target_tip,
                            tuple(tip for _plan_name, tip in selection.sources),
                            (chain.step_trees[-1] if chain.step_trees else
                             gitops.tree_of(main, target_tip)),
                            digest, shared_before, 1,
                            (f"Conflict while merging {chain.conflict_plan}",
                             *(f"{chain.conflict_plan}:{item}"
                               for item in chain.conflicts))))
                    return 1
                batch_sources = tuple(selection.sources)
                batch_step_trees = chain.step_trees
                batch_plans = selection.plan_names
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
                    refusal = ("every queued plan conflicts, so no independent "
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
                batch_plans = filtered.plan_names
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
            failure_summary = "" if result.returncode == 0 else _failure_summary(
                result, source_worktrees, batch_sources)
            if status == "FAILED" and bisect:
                bisected = bisect_batch_failure(
                    main, target_tip, batch_sources, script, failure_summary,
                    source_worktrees, label)
                failure_summary = _report_localization(
                    assent_dir, batch_plans, bisected,
                    batch_plans if exact else None, label)
                localized = True
                if bisected.kept:
                    sources = bisected.kept
                    step_trees = bisected.kept_step_trees
                    status, exit_code = "PASSED", 0

        receipt = _new_batch_receipt(
            status=status, target_tip=target_tip, sources=sources,
            step_trees=step_trees, digest=digest,
            shared_inputs=_shared_digest(main, contracts, sources),
            exit_code=exit_code, failure_summary=failure_summary)

        changed = _batch_drift(
            configs, main, excludes, target_branch, target_tip,
            batch_sources, script, digest,
            contracts, selection.sources, shared_before, source_worktrees)
        if changed:
            if localized:
                print(f"{label}: the repository changed while the batch "
                      "was being verified, so the localization above certifies "
                      "nothing and the receipt records the drift instead")
            receipt = _new_batch_receipt(
                status="FAILED", target_tip=target_tip, sources=batch_sources,
                step_trees=batch_step_trees, digest=digest,
                shared_inputs=shared_before,
                exit_code=1, failure_summary="; ".join(changed))

        write_batch_receipt(path, receipt, main)

        if action_results is not None:
            action_results.append(_batch_evidence(receipt, reused=False))

    for source in receipt.sources:
        print(f"  {source.plan}: source {source.source_tip[:12]} "
              f"-> tree {source.step_tree}")
    if receipt.status == "PASSED":
        if not localized:
            print(f"{label}: passed ({receipt.final_tree})")
            if batch_skipped:
                print(f"{label}: verified " + ", ".join(batch_plans)
                      + "; skipped " + ", ".join(batch_skipped))
            return 0
        # The batch as requested did not pass, so the exit code stays nonzero
        # even though a smaller batch is now certified: a caller must not read
        # success for plans that were just proven, or suspected, broken.
        print(f"{label}: failed, but {len(receipt.sources)} plan(s) "
              f"kept a PASSED batch receipt ({receipt.final_tree}); "
              "`assent accept --all` publishes exactly those")
        return 1
    print(f"{label}: failed ({receipt.failure_summary.splitlines()[0]})")
    # Only the first stored line is echoed above, so an ignored-input hint is
    # printed explicitly rather than left for a human to find in the receipt.
    print_ignored_input_diagnosis(label, receipt.failure_summary)
    return 1


def verify_batch(config_path: str, assent_dir: str | Path, bisect: bool = True,
                 confirm: Callable[[str], bool] = confirm_on_terminal) -> int:
    """Verify every queued plan as one candidate; zero only for PASSED.

    An empty batch is success with no receipt: there is nothing to certify, so
    inventing a receipt would be inventing evidence.

    Sources that conflict with the candidate are the one place a human is asked
    a question: ``confirm`` decides once whether to verify the independent
    subset that remains, or nothing at all.  It is a parameter so a test can
    answer without a terminal; the CLI leaves it at the ``input``-based default.

    A failed batch is localized to the plan that breaks it, and the plans
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
                          plan_names: Sequence[str], bisect: bool = True) -> int:
    """Verify exactly the named plans as one dependency-ordered candidate.

    Unlike the dynamic batch path, a selected conflict is a refusal rather than
    an invitation to skip plans.  The requested set is never broadened or
    silently reduced, and a smaller PASSED prefix from bisection still returns
    nonzero because it does not certify the original request.
    """
    assent_dir = Path(assent_dir)
    try:
        with hold_integration_lock(assent_dir):
            return _verify_batch_locked(
                config_path, assent_dir, bisect, confirm_on_terminal, plan_names)
    except LockBusy as e:
        print(f"verify selected: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify selected: failed ({e})")
        return 1


def verify_selected_batch_action(
        config_path: str, assent_dir: str | Path, plan_names: Sequence[str], *,
        recheck: bool = False) -> FullVerifyEvidence:
    """Run or reuse one exact selected transaction and return typed evidence."""
    assent_dir = Path(assent_dir)
    results: list[FullVerifyEvidence] = []
    try:
        with hold_integration_lock(assent_dir):
            _verify_batch_locked(
                config_path, assent_dir, False, confirm_on_terminal, plan_names,
                action_results=results, recheck=recheck)
    except LockBusy as e:
        print(f"verify selected: refused ({e})")
        detail = str(e)
    except AssentError as e:
        print(f"verify selected: failed ({e})")
        detail = str(e)
    else:
        if results:
            return results[-1]
        detail = "selected verification ended without typed evidence"
    return FullVerifyEvidence(
        "INFRASTRUCTURE_FAILED", tuple(plan_names), "", (), "", "", "", 1,
        (detail,))


# Keep both word orders discoverable for library callers while the CLI uses the
# explicit ``verify_selected_batch`` name.
verify_batch_selected = verify_selected_batch
