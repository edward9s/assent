"""Batch publication of several verified plans in one transaction.

``assent accept --all`` (``accept_all`` below) is the only acceptance path that
may run a verifier: when no batch evidence applies, it refreshes each finished
plan's stale receipt in dependency order and publishes it through the
unchanged direct ``accept_plan``.

``accept --all`` also owns the batch release path.  When a fresh PASSED batch
receipt covers several plans, their whole merge chain is replayed in one
temporary worktree, compared tree by tree against that receipt, and published
by a single ref update -- all of them or none of them.

``assent accept PLAN_A PLAN_B`` is the explicit selected-batch path.  It
requires a fresh receipt for exactly those dependency-normalized plans and
uses the same replay and publication machinery as ``accept --all`` without
verification or per-plan fallback.

That receipt may cover only part of what is finished: ``verify --batch`` leaves
out a plan whose source conflicts, together with everything queued after it,
so the recorded chain can have gaps in the publishing order.  The release
publishes exactly the sources the receipt records and then names every finished
plan it did not cover, which is what keeps its zero exit code from reading as
"everything is published".

This module depends on ``assent.accept`` for the direct transaction and its
public acceptance helpers; that dependency runs one way only, so the direct
path never loads the batch implementation.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from assent import AssentError, gitops, verification
from assent.accept import (accept_plan, accept_merge_message, cleanup_warning,
                           dependency_tip)
from assent.config import Config, load_config
from assent.plan_source import resolve_source_snapshot
from assent.plandeps import (infer_plan_completion, live_upstreams,
                               order_plans_by_dependency,
                               parse_plan_dependencies,
                               parse_plan_dependency_graph)
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock

# Both --all paths announce themselves before doing any work, so which one a run
# took is always readable from its output instead of having to be inferred.
_BATCH_BANNER = (
    "accept --all: batch release; the whole batch is published by one ref "
    "update or not at all (all or nothing on failure).")
_PER_PLAN_BANNER = (
    "accept --all: per-plan verify+accept; on failure the chain stops and "
    "the plans already published stay published.")


def _already_integrated(cfg: Config) -> bool:
    """Best-effort check for a plan whose current source is already an
    ancestor of the current target -- i.e. a prior accept already published
    it and nothing has changed since.

    This lets a rerun route straight to ``accept_plan``'s own idempotent
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
        _branch, source_tip, _worktree = resolve_source_snapshot(
            main, cfg.tasks_name, cfg.git_excludes)
    except AssentError:
        return False
    return gitops.is_ancestor(main, source_tip, target_tip)


def _no_source_remains(main: Path, plan_name: str) -> bool:
    """True when a plan's ``<plan>/*`` worktree and branches are both gone.

    ``assent clean`` only deletes a plan's branch once it has proven the
    branch is fully merged into the integration target, so the branch's
    absence is itself the merge evidence -- ``accept --all`` does not need a
    fresh proof to skip it.  A branch deleted by hand outside ``clean`` is not
    covered by this contract, matching ``clean``'s own existing stance.
    """
    return (gitops.plan_worktree(main, plan_name) is None
            and not gitops.plan_branches(main, plan_name))


def _selected_batch_order(assent_dir: Path,
                          plan_names: Sequence[str]) -> tuple[str, ...]:
    """Normalize explicit plan names using the current dependency graph."""
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
    return tuple(order_plans_by_dependency(graph, set(names)))


def _selected_accept_label(plan_names: Sequence[str]) -> str:
    """Format the selected command mode for diagnostics."""
    return "accept " + " ".join(plan_names)


def _selected_verify_command(plan_names: Sequence[str]) -> str:
    """Format the exact verifier command that rebuilds a selected receipt."""
    return "assent verify " + " ".join(plan_names)


@dataclass(frozen=True)
class _BatchSource:
    """One batched plan's live Git identity beside its recorded step tree."""

    plan: str
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
    conflict_plan: str = ""
    conflicts: tuple[str, ...] = ()
    published: bool = False


def _batch_source_identities(
        main: Path, receipt: verification.BatchVerificationReceipt,
        configs: dict[str, Config]) -> tuple[list[_BatchSource], str | None]:
    """Resolve every batched plan's current source, refusing on any drift.

    ``resolve_source_snapshot`` is the same resolution a single-plan accept
    performs, so a detached, foreign, ambiguous, or dirty source refuses the
    batch for exactly the reasons it would refuse one plan.
    """
    sources: list[_BatchSource] = []
    for entry in receipt.sources:
        try:
            branch, tip, worktree = resolve_source_snapshot(
                main, entry.plan, configs[entry.plan].git_excludes)
        except AssentError as e:
            return [], f"{entry.plan}: {e}"
        if tip != entry.source_tip:
            return [], (f"{entry.plan}: source tip changed since the batch was "
                        f"verified ({entry.source_tip[:12]} -> {tip[:12]})")
        sources.append(
            _BatchSource(entry.plan, branch, tip, worktree, entry.step_tree))
    return sources, None


def _batch_gate_problem(main: Path, configs: dict[str, Config],
                        sources: Sequence[_BatchSource], target_branch: str,
                        target_before: str,
                        receipt: verification.BatchVerificationReceipt
                        ) -> str | None:
    """Re-observe every identity a batch release is about to publish over.

    This is the single-plan accept gate widened to cover all sources at once:
    the target must still be the branch and the commit the chain was replayed
    onto, the main worktree must still be clean, and every source branch and
    worktree must still be exactly what the receipt certified.  It runs both
    before and after the replay, so a concurrent change during the replay is
    caught while the target is still untouched.
    """
    main_excludes = configs[sources[0].plan].git_excludes
    if gitops.current_branch(main) != target_branch:
        return (f"the main worktree {main} is no longer on {target_branch} "
                "(concurrent branch switch during the batch release)")
    if gitops.commit_of(main, "HEAD") != target_before:
        return (f"{target_branch} moved during the batch release (concurrent "
                "update); refusing to overwrite it")
    if not gitops.working_tree_status(main, main_excludes).is_clean:
        return f"the main worktree {main} is not clean"
    for source in sources:
        excludes = configs[source.plan].git_excludes
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
        # contains; asserting it here keeps a merged plan out of the replayed
        # chain, where its merge would collapse into a no-op commit shape.
        if gitops.is_ancestor(main, source.tip, target_before):
            return (f"{source.plan} ({source.tip[:12]}) is already contained in "
                    f"{target_branch}, so the recorded chain is not the chain "
                    "being published")
    try:
        current = verification.verifier_digest(configs[sources[0].plan])
    except AssentError as e:
        return str(e)
    if current != receipt.verify_script_sha256:
        return "the verification script changed during the batch release"
    # The shared inputs are evidence like the source, target, and verifier are,
    # so they are re-observed at the same two moments.  This only reads and
    # classifies: acceptance never provisions or repairs a link, and never
    # invokes AI, to make a stale receipt publishable.
    try:
        if verification.current_batch_shared_inputs(
                main, receipt) != receipt.shared_inputs_sha256:
            return ("the reviewed shared inputs changed during the batch "
                    "release")
    except AssentError as e:
        return f"the reviewed shared inputs can no longer be proven: {e}"
    return None


def _batch_prerequisite_problem(main: Path, assent_dir: Path,
                                configs: dict[str, Config],
                                sources: Sequence[_BatchSource],
                                target_before: str) -> str | None:
    """Refuse a batch that would publish a plan ahead of an upstream it needs.

    A single-plan accept refuses to publish a downstream whose prerequisite is
    not provably in the target.  A batch satisfies the same requirement in one of
    two ways: the upstream is earlier in this very chain, or it is already
    accepted.  Anything else would carry unverified upstream commits into the
    target through a downstream merge, which is exactly what the single-plan
    gate exists to prevent.
    """
    earlier: set[str] = set()
    for source in sources:
        excludes = configs[source.plan].git_excludes
        dependencies = parse_plan_dependencies(configs[source.plan].tasks_dir)
        for dependency in live_upstreams(assent_dir, dependencies):
            if dependency in earlier:
                continue
            tip, problem = dependency_tip(main, dependency, excludes)
            if problem is not None:
                return (f"prerequisite {dependency} of {source.plan} cannot be "
                        f"proven accepted ({problem})")
            if not gitops.is_ancestor(main, tip, target_before):
                return (f"prerequisite {dependency} of {source.plan} "
                        f"({tip[:12]}) is neither part of this batch nor already "
                        "accepted into the target")
        earlier.add(source.plan)
    return None


def _replay_batch_chain(candidate: Path, target_branch: str,
                        receipt: verification.BatchVerificationReceipt,
                        sources: Sequence[_BatchSource],
                        replay: _BatchReplay) -> None:
    """Rebuild the verified chain in an open temporary worktree, step by step.

    Every step is compared against the receipt as soon as it exists, so a chain
    that has stopped being the verified one is abandoned before the next merge
    instead of at the end.  Trees do not depend on commit messages, so replaying
    with the audit messages a single-plan accept writes reproduces exactly the
    trees ``verify --batch`` recorded.
    """
    for source in sources:
        message = accept_merge_message(
            target_branch, source.plan, source.branch, source.tip,
            source.step_tree, receipt.verify_script_sha256)
        previous = gitops.commit_of(candidate, "HEAD")
        outcome = gitops.merge_no_ff(candidate, source.tip, message)
        if not outcome.ok:
            replay.conflict_plan = source.plan
            replay.conflicts = outcome.conflicts
            return
        if gitops.commit_parents(candidate, "HEAD") != (previous, source.tip):
            replay.problem = (f"replaying {source.plan} did not produce the "
                              "expected two-parent no-ff merge")
            return
        tree = gitops.tree_of(candidate, "HEAD")
        if tree != source.step_tree:
            replay.problem = (f"replayed tree after {source.plan} is {tree}, "
                              f"not the verified {source.step_tree}")
            return
        replay.merges.append(
            (source.plan, gitops.commit_of(candidate, "HEAD")))
    final_tree = gitops.tree_of(candidate, "HEAD")
    if final_tree != receipt.final_tree:
        replay.problem = (f"replayed final tree is {final_tree}, not the verified "
                          f"{receipt.final_tree}")
        return
    replay.chain_head = gitops.commit_of(candidate, "HEAD")


def _unbatched_finished_plans(config_path: str, assent_dir: Path, main: Path,
                                published: Sequence[str],
                                target_tip: str) -> list[str]:
    """Finished plans this batch did not cover, in the usual publishing order.

    A batch receipt describes only the plans it was built from.  A finished
    plan can be outside it for two reasons that look identical here -- it
    finished after the batch was verified, or building the batch had to leave it
    out -- so this reports membership and never guesses which reason applies.
    """
    try:
        graph = parse_plan_dependency_graph(assent_dir)
        candidates = {plan_name for plan_name in graph
                      if plan_name not in set(published)
                      and infer_plan_completion(assent_dir / plan_name).complete}
        order = order_plans_by_dependency(graph, candidates)
    except AssentError:
        return []
    outstanding: list[str] = []
    for plan_name in order:
        if _no_source_remains(main, plan_name):
            continue
        try:
            cfg = load_config(config_path, plan_name)
            _branch, tip, _worktree = resolve_source_snapshot(
                main, plan_name, cfg.git_excludes)
        except AssentError:
            outstanding.append(plan_name)
            continue
        if not gitops.is_ancestor(main, tip, target_tip):
            outstanding.append(plan_name)
    return outstanding


def _release_batch(config_path: str, assent_dir: Path) -> int | None:
    """Publish a fresh batch receipt's whole chain; ``None`` means fall back.

    ``accept --all`` chooses its path here and nowhere else.  A missing receipt,
    and an expired one, both return ``None`` after saying so, which hands the run
    to the unchanged per-plan path; only an unreadable receipt is a refusal,
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


def accept_selected_batch(config_path: str, assent_dir: Path,
                          plan_names: Sequence[str]) -> int:
    """Publish only a fresh receipt for the exact selected plan set."""
    assent_dir = Path(assent_dir)
    requested = tuple(plan_names)
    mode = _selected_accept_label(requested)
    try:
        with hold_integration_lock(assent_dir):
            result = _release_batch_locked(
                config_path, assent_dir,
                verification.batch_receipt_path(assent_dir),
                requested_plans=requested)
            assert result is not None
            return result
    except LockBusy as e:
        print(f"{mode}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"{mode}: failed ({e})")
        return 1


def _release_batch_locked(config_path: str, assent_dir: Path,
                          path: Path,
                          requested_plans: Sequence[str] | None = None
                          ) -> int | None:
    """Gate, replay, and publish one batch with the integration lock held."""
    main = gitops.main_worktree(assent_dir.parent)
    selected = requested_plans is not None
    requested = tuple(requested_plans or ())
    mode = "accept --all"
    verify_hint = "assent verify --batch"
    expected_plans: tuple[str, ...] = ()
    if selected:
        mode = _selected_accept_label(requested)
        verify_hint = _selected_verify_command(requested)
        try:
            expected_plans = _selected_batch_order(assent_dir, requested)
        except AssentError as e:
            print(f"{mode}: refused, {e}. Run `{verify_hint}` to create a "
                  "fresh receipt for this exact selected set; accept never "
                  "runs verification")
            return 1
        mode = _selected_accept_label(expected_plans)
        verify_hint = _selected_verify_command(expected_plans)
    try:
        receipt = verification.read_batch_receipt(path, main)
    except AssentError as e:
        if selected:
            print(f"{mode}: refused, the batch verification receipt is "
                  f"unusable ({e}). Keep every source and the target unchanged; "
                  f"run `{verify_hint}` to create a fresh receipt for this exact "
                  "selected set")
        else:
            print(f"accept --all: refused, the batch verification receipt is "
                  f"unusable ({e}). It is derived evidence: delete {path} and run "
                  "`assent verify --batch` again")
        return 1
    if selected and receipt.plan_names != expected_plans:
        print(f"{mode}: refused, the batch verification receipt covers "
              f"{', '.join(receipt.plan_names)}, not the exact selected set "
              f"{', '.join(expected_plans)}. Keep every source and the target "
              f"unchanged; run `{verify_hint}` to create the matching receipt")
        return 1
    try:
        # The batch's own order is fixed in the receipt, but a plan whose dependency
        # graph stopped parsing is refused on both paths rather than published by
        # whichever one happens not to read it.
        parse_plan_dependency_graph(assent_dir)
    except AssentError as e:
        print(f"{mode}: refused, plan dependency graph is invalid ({e})")
        return 1
    configs = {plan_name: load_config(config_path, plan_name)
               for plan_name in receipt.plan_names}
    cfg = configs[receipt.plan_names[0]]

    if receipt.status != "PASSED":
        reasons: tuple[str, ...] = (f"its status is {receipt.status}",)
    else:
        try:
            reasons = verification.batch_receipt_staleness(cfg, receipt)
        except AssentError as e:
            reasons = (f"its freshness could not be proven: {e}",)
    if reasons:
        if selected:
            print(f"{mode}: refused, the batch verification receipt is not fresh ("
                  + "; ".join(reasons) + f"). Keep every source and the target "
                  f"unchanged; run `{verify_hint}` to build a fresh receipt")
            return 1
        print("accept --all: the batch verification receipt has expired ("
              + "; ".join(reasons) + "); deleted, because a receipt is derived "
              "and disposable. Run `assent verify --batch` to build a new one.")
        verification.invalidate_batch_receipt(assent_dir)
        return None

    if selected:
        print(f"{mode}: batch release; the exact selected batch is published by "
              "one ref update or not at all (all or nothing on failure).")
    else:
        print(_BATCH_BANNER)
    print(f"  batch receipt covers: {', '.join(receipt.plan_names)}")

    with ExitStack() as locks:
        # The integration lock is already held; taking every batched plan's
        # own lock next keeps the fixed integration-then-plan order, and a
        # plan that is currently running refuses the whole release.
        for plan_name in receipt.plan_names:
            locks.enter_context(hold_lock(configs[plan_name].tasks_dir, plan_name))

        unfinished: list[str] = []
        for plan_name in receipt.plan_names:
            completion = infer_plan_completion(assent_dir / plan_name)
            if not completion.complete:
                unfinished.append(f"{plan_name} ({completion.reason})")
        if unfinished:
            print(f"{mode}: refused, batched plan(s) are no longer "
                  f"finished: {', '.join(unfinished)}. The target was not "
                  f"changed; run `{verify_hint}` again")
            return 1

        sources, drift = _batch_source_identities(main, receipt, configs)
        if drift is not None:
            print(f"{mode}: refused, {drift}. The target was not changed; "
                  f"run `{verify_hint}` again")
            return 1

        target_branch = gitops.require_current_branch(main)
        target_before = gitops.commit_of(main, "HEAD")
        problem = _batch_prerequisite_problem(
            main, assent_dir, configs, sources, target_before)
        if problem is not None:
            print(f"{mode}: refused, {problem}. The target was not changed; "
                  f"accept the prerequisite first, or rebuild the batch with "
                  f"`{verify_hint}` once it is finished")
            return 1

        problem = _batch_gate_problem(
            main, configs, sources, target_branch, target_before, receipt)
        if problem is not None:
            print(f"{mode}: refused, {problem}. The target was not "
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
                    # target move -- there is no per-plan ref update that
                    # could leave half a batch published.
                    replay.problem = _batch_gate_problem(
                        main, configs, sources, target_branch, target_before,
                        receipt) or ""
                    if not replay.problem:
                        gitops.fast_forward(main, replay.chain_head)
                        replay.published = True
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
            if not replay.published and not replay.problem \
                    and not replay.conflict_plan:
                return 1

        if replay.conflict_plan:
            print(f"{mode}: refused, replaying {replay.conflict_plan} "
                  f"into {target_branch} conflicts even though the batch "
                  "verified cleanly; the target was not changed. Conflicting "
                  "file(s):")
            for conflicting in replay.conflicts:
                print(f"  - {conflicting}")
            print(f"Run `{verify_hint}` to rebuild the batch candidate")
            return 1
        if replay.problem:
            print(f"{mode}: refused, {replay.problem}. The target was not "
                  f"changed and every source was kept; run `{verify_hint}` to "
                  "rebuild the batch candidate")
            return 1

        consume_error = ""
        try:
            verification.invalidate_batch_receipt(assent_dir)
        except AssentError as e:
            consume_error = str(e)

        target_after = gitops.commit_of(main, "HEAD")
        if selected:
            print(f"{mode}: batch release done, {len(replay.merges)} plan(s) "
                  "published by one ref update without running verification.")
        else:
            print(f"accept --all: batch release done, {len(replay.merges)} plan(s) "
                  "published by one ref update without running verification.")
        print(f"  target branch:   {target_branch}")
        print(f"  target before:   {target_before}")
        print(f"  target after:    {target_after}")
        print(f"  verified tree:   {receipt.final_tree}")
        print(f"  verifier digest: {receipt.verify_script_sha256}")
        print("  audit merges (one per plan, same shape and message as a "
              "single-plan accept):")
        for source, (plan_name, commit) in zip(sources, replay.merges):
            print(f"    {plan_name}: source {source.tip} -> merge {commit} "
                  f"(tree {source.step_tree})")
        print(f"  accepted:  {', '.join(receipt.plan_names)}")
        # Stated at the left margin, not inside the detail block: a partial
        # release exits zero, so the plans it did not publish are the one
        # thing a reader must not miss.
        if not selected:
            outstanding = _unbatched_finished_plans(
                config_path, assent_dir, main, receipt.plan_names, target_after)
            if outstanding:
                print(f"accept --all: {len(outstanding)} finished plan(s) still "
                      f"not accepted: {', '.join(outstanding)}.")
                print("  A batch publishes only what it was built from; a plan "
                      "is outside it either because it finished later or because "
                      "the batch was built without it. This run published the "
                      "batch alone and neither verified nor examined them; run "
                      "`assent verify --batch` to build the next batch over what "
                      "remains.")
        if consume_error:
            print(f"warning: the batch was published but its receipt could not "
                  f"be deleted ({consume_error}); delete {path} by hand. It "
                  "describes published work, so it can no longer authorize a "
                  "release.")
        else:
            if selected:
                print(f"  batch receipt consumed; rerunning `{mode}` is a no-op "
                      "for the plans it published.")
            else:
                print("  batch receipt consumed; rerunning `assent accept --all` is "
                      "a no-op for the plans it published.")
        print("  source branch/worktree kept for every published plan; retain "
              "each while a dependent may still need its source evidence. "
              "`assent clean PLAN` makes the final safety decision.")
        print("  The integration lock coordinates Assent commands only; do not "
              "run concurrent external Git writes during acceptance.")
        return 0


def accept_all(config_path: str, assent_dir: Path) -> int:
    """Publish one verified batch, or verify-then-accept every plan serially.

    ``--all`` picks its own path and always says which one it took: a fresh
    PASSED batch receipt is released as one transaction (``_release_batch``),
    and anything else -- no receipt, or an expired one -- runs the per-plan
    path below, whose behavior is unchanged.

    Per-plan path: verify-then-accept every finished plan, serially,
    fail-closed.

    Selection and ordering reuse ``plandeps`` exactly as whole-project ``run``
    does. Each plan refreshes its verification receipt only when stale
    (``verify_plan_if_needed``, the same unattended full verification as
    ``assent verify PLAN``); that shared plan-verification entry point also
    performs the one post-verification best-effort report refresh. The chain
    then reuses ``accept_plan`` unchanged. A
    plan already published by a prior run skips straight to
    ``accept_plan``'s own idempotent path (see ``_already_integrated``).
    A finished plan whose source branch and worktree have both already
    been cleaned away (``_no_source_remains``) is skipped instead of run
    through verify/accept, since there is nothing left to verify; this
    skip does not count as a chain failure. Only ``--all`` skips this way --
    a directly named ``accept PLAN`` still fails closed on a missing
    source, since a directly named target must never be silently skipped.
    The first real failure stops the remaining chain; plans already
    published stay published.
    """
    assent_dir = Path(assent_dir)
    released = _release_batch(config_path, assent_dir)
    if released is not None:
        return released
    print(_PER_PLAN_BANNER)

    try:
        graph = parse_plan_dependency_graph(assent_dir)
    except AssentError as e:
        print(f"accept --all: refused, plan dependency graph is invalid ({e})")
        return 1
    if not graph:
        print("accept --all: no plan with a task file found.")
        return 0

    finished: set[str] = set()
    for plan_name in graph:
        completion = infer_plan_completion(assent_dir / plan_name)
        if completion.complete:
            finished.add(plan_name)
        else:
            print(f"accept --all: skip {plan_name} (not finished: {completion.reason})")
    if not finished:
        print("accept --all: no finished plan to accept.")
        return 0

    try:
        order = order_plans_by_dependency(graph, finished)
    except AssentError as e:
        print(f"accept --all: refused, {e}")
        return 1

    accepted: list[str] = []
    skipped: list[str] = []
    failure: tuple[str, str] | None = None
    processed = 0
    for plan_name in order:
        processed += 1
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError as e:
            failure = (plan_name, f"config error: {e}")
            break
        main = gitops.main_worktree(cfg.root)
        if _no_source_remains(main, plan_name):
            print(f"accept --all: skip {plan_name} (no source branch remains; "
                  "already integrated and cleaned)")
            skipped.append(plan_name)
            continue
        if (not _already_integrated(cfg)
                and verification.verify_plan_if_needed(cfg) != 0):
            failure = (plan_name, "verification refused or failed")
            break
        if accept_plan(cfg) != 0:
            failure = (plan_name, "accept refused or failed")
            break
        accepted.append(plan_name)

    remaining = order[processed:]
    print("accept --all: summary")
    print(f"  accepted:  {', '.join(accepted) if accepted else '(none)'}")
    if skipped:
        print(f"  skipped:   {', '.join(skipped)} "
              "(no source remains; already integrated and cleaned)")
    if failure is not None:
        plan_name, reason = failure
        print(f"  failed:    {plan_name} ({reason})")
    print(f"  remaining: {', '.join(remaining) if remaining else '(none)'}")
    return 1 if failure is not None else 0
