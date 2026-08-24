"""One plan verified against the integration target, and its receipt.

The per-plan receipt (``<plan>/_verification.toml``) is a derived runtime
cache.  It records facts about one explicit source snapshot, the resulting
integration tree, and the main-tree verification script; it is not human
approval and never advances a Git ref.

This module owns that receipt end to end -- parsing, candidate reconstruction,
the unattended verification run, freshness, and the report lines -- and knows
nothing about the batch receipt, which is a separate model in
``assent.batch_receipt``.
"""
from __future__ import annotations

import subprocess
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError, gitops, shared_paths
from assent.config import Config
from assent.plandeps import (infer_plan_completion, live_upstreams,
                               parse_plan_dependencies)
from assent.init import recover_expanded_bridge_drift
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan
from assent.verification_common import (DIGEST_RE, RECEIPT_STATUSES,
                                        SUMMARY_LIMIT, VERIFY_COMMAND,
                                        CandidateConflict, FullVerifyEvidence,
                                        atomic_write_text, candidate_tree,
                                        ignored_input_diagnosis,
                                        invalidate_receipt,
                                        provisioned_candidate_links,
                                        require_oid, run_full_verifier,
                                        sha256_file, source_snapshot, summary,
                                        toml_string, union_worktree_links)

RECEIPT_NAME = "_verification.toml"
# 2 adds shared_inputs_sha256.  The bump is fail-closed on purpose: a version-1
# receipt recorded no shared-input evidence at all, so it is unusable rather
# than silently upgraded with an assumed-empty digest.
RECEIPT_VERSION = 2
_COMPLETE_STATUSES = ("DONE", "SKIP")
# The receipt records a source/target conflict with this prefix, which is also
# what tells a failed `verify PLAN` to point at `assent reconcile`.
_CONFLICT_SUMMARY_PREFIX = "Integration conflict: "
_RECEIPT_KEYS = {
    "version", "status", "source_tip", "target_tip", "integration_tree",
    "verify_script_sha256", "shared_inputs_sha256", "verify_command",
    "exit_code", "completed_at", "failure_summary",
}
# The evidence a shared-input digest changing produces, phrased so a human sees
# the remedy without opening the receipt.
_SHARED_INPUT_DRIFT = (
    "a declared shared input changed while the full verifier was running, so "
    "the run certifies nothing")


@dataclass(frozen=True)
class VerificationReceipt:
    version: int
    status: str
    source_tip: str
    target_tip: str
    integration_tree: str
    verify_script_sha256: str
    verify_command: str
    exit_code: int
    completed_at: str
    failure_summary: str
    #: Digest of every reviewed shared input this verification depended on --
    #: the selected profiles and the exact content of their declared targets.
    #: The empty default is what a receipt written before this schema carried,
    #: and it is never a usable value: validation refuses it on the way in and
    #: on the way out, so an absent digest is stale evidence, not an assumed
    #: empty one.
    shared_inputs_sha256: str = ""


def receipt_path(cfg: Config) -> Path:
    """Return the explicitly selected plan's derived receipt path."""
    return cfg.tasks_dir / RECEIPT_NAME


def invalidate_plan_receipt(cfg: Config) -> bool:
    """Delete one plan's receipt so no accept can consume it; True if one existed.

    Every command that changes what the receipt was written against uses this
    rather than unlinking the file itself.  The receipt is derived and
    disposable, so deleting it only ever costs one ``assent verify PLAN``.
    """
    path = receipt_path(cfg)
    existed = path.exists()
    invalidate_receipt(path)
    return existed


def _receipt_text(receipt: VerificationReceipt) -> str:
    return (
        f"version = {receipt.version}\n"
        f"status = {toml_string(receipt.status)}\n"
        f"source_tip = {toml_string(receipt.source_tip)}\n"
        f"target_tip = {toml_string(receipt.target_tip)}\n"
        f"integration_tree = {toml_string(receipt.integration_tree)}\n"
        f"verify_script_sha256 = {toml_string(receipt.verify_script_sha256)}\n"
        f"shared_inputs_sha256 = {toml_string(receipt.shared_inputs_sha256)}\n"
        f"verify_command = {toml_string(receipt.verify_command)}\n"
        f"exit_code = {receipt.exit_code}\n"
        f"completed_at = {toml_string(receipt.completed_at)}\n"
        f"failure_summary = {toml_string(receipt.failure_summary)}\n"
    )


def _validate_receipt(receipt: VerificationReceipt, repository: Path) -> None:
    if type(receipt.version) is not int or receipt.version != RECEIPT_VERSION:
        raise AssentError(
            f"Verification receipt version must be {RECEIPT_VERSION}")
    if receipt.status not in RECEIPT_STATUSES:
        raise AssentError("Verification receipt status must be PASSED or FAILED")
    for name in ("source_tip", "target_tip", "integration_tree"):
        require_oid(getattr(receipt, name), name, "Verification receipt")
    if not isinstance(receipt.verify_script_sha256, str) or not DIGEST_RE.fullmatch(
            receipt.verify_script_sha256):
        raise AssentError(
            "Verification receipt verify_script_sha256 must be a 64-character "
            "lowercase hexadecimal digest")
    if not isinstance(receipt.shared_inputs_sha256, str) or not DIGEST_RE.fullmatch(
            receipt.shared_inputs_sha256):
        raise AssentError(
            "Verification receipt shared_inputs_sha256 must be a 64-character "
            "lowercase hexadecimal digest")
    if receipt.verify_command != VERIFY_COMMAND:
        raise AssentError(
            f"Verification receipt verify_command must be {VERIFY_COMMAND!r}")
    if type(receipt.exit_code) is not int:
        raise AssentError("Verification receipt exit_code must be an integer")
    if not isinstance(receipt.completed_at, str):
        raise AssentError("Verification receipt completed_at must be a string")
    try:
        completed = datetime.fromisoformat(receipt.completed_at)
    except ValueError as e:
        raise AssentError(
            "Verification receipt completed_at must be ISO 8601") from e
    if completed.tzinfo is None:
        raise AssentError("Verification receipt completed_at must include a timezone")
    if not isinstance(receipt.failure_summary, str):
        raise AssentError("Verification receipt failure_summary must be a string")
    if len(receipt.failure_summary) > SUMMARY_LIMIT:
        raise AssentError("Verification receipt failure_summary exceeds the size limit")
    if receipt.status == "PASSED" and receipt.exit_code != 0:
        raise AssentError("A PASSED verification receipt must have exit_code = 0")
    if receipt.status == "FAILED" and receipt.exit_code == 0:
        raise AssentError("A FAILED verification receipt must have a nonzero exit_code")
    expected_types = {
        "source_tip": "commit",
        "target_tip": "commit",
        "integration_tree": "tree",
    }
    for name, expected in expected_types.items():
        actual = gitops.object_type(repository, getattr(receipt, name))
        if actual != expected:
            raise AssentError(
                f"Verification receipt {name} names a Git {actual}, not a {expected}")


def write_receipt(path: Path, receipt: VerificationReceipt,
                  repository: Path) -> None:
    """Atomically replace a receipt after validating its schema and Git objects."""
    path = Path(path)
    _validate_receipt(receipt, repository)
    atomic_write_text(path, _receipt_text(receipt))


def read_receipt(path: Path, repository: Path) -> VerificationReceipt:
    """Read a receipt fail-closed, including object existence and type checks."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as e:
        raise AssentError(f"Verification receipt not found: {path}") from e
    except OSError as e:
        raise AssentError(f"Unable to read verification receipt {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AssentError(f"Verification receipt is not valid TOML ({path}): {e}") from e
    if not isinstance(data, dict):
        raise AssentError("Verification receipt must be a TOML table")
    unknown = sorted(set(data) - _RECEIPT_KEYS)
    missing = sorted(_RECEIPT_KEYS - set(data))
    if unknown:
        raise AssentError(
            f"Verification receipt has unknown keys: {', '.join(unknown)}")
    if missing:
        raise AssentError(
            f"Verification receipt is missing keys: {', '.join(missing)}")
    try:
        receipt = VerificationReceipt(**data)
    except TypeError as e:
        raise AssentError(f"Verification receipt has an invalid structure: {e}") from e
    _validate_receipt(receipt, repository)
    return receipt


# Explicit names make the artifact API discoverable while keeping concise aliases.
read_verification_receipt = read_receipt
write_verification_receipt = write_receipt


def _stack_sources(cfg: Config, target_tip: str,
                   downstream_tip: str) -> tuple[gitops.PlanSourceSnapshot, ...]:
    """Snapshot the declared live base and prove the downstream contains it.

    Only direct dependencies participate here.  An unrelated malformed plan
    must not invalidate this plan's receipt, while every declared live
    upstream is required to be complete.  Only the explicitly declared base
    contributes a source identity or ancestry requirement; non-base ``after``
    entries provide ordering only.

    An archived upstream is proven complete by the roster and has no source
    left to snapshot, so it is filtered out first (see ``live_upstreams``).
    """
    dependencies = parse_plan_dependencies(cfg.tasks_dir)
    sources: list[gitops.PlanSourceSnapshot] = []
    for plan_name in live_upstreams(cfg.assent_dir, dependencies):
        completion = infer_plan_completion(cfg.assent_dir / plan_name)
        if not completion.complete:
            raise AssentError(
                f"upstream plan {plan_name} is incomplete: {completion.reason}")
        if dependencies.base != plan_name:
            continue
        source = gitops.resolve_plan_source(
            cfg.root, plan_name, cfg.git_excludes)
        sources.append(source)
        if not gitops.is_ancestor(cfg.root, source.tip, downstream_tip):
            raise AssentError(
                f"stale stack for {cfg.tasks_name}: current upstream {plan_name} tip "
                f"{source.tip} is not an ancestor of downstream tip "
                f"{downstream_tip}; the downstream source and existing receipt "
                f"were preserved. Run `assent rework {cfg.tasks_name}` after "
                "deciding how to handle the upstream change, or replan the "
                "dependency")
    return tuple(sources)


def _new_receipt(*, status: str, source_tip: str, target_tip: str,
                 integration_tree: str, digest: str, shared_inputs: str,
                 exit_code: int,
                 failure_summary: str = "") -> VerificationReceipt:
    return VerificationReceipt(
        version=RECEIPT_VERSION,
        status=status,
        source_tip=source_tip,
        target_tip=target_tip,
        integration_tree=integration_tree,
        verify_script_sha256=digest,
        shared_inputs_sha256=shared_inputs,
        verify_command=VERIFY_COMMAND,
        exit_code=exit_code,
        completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        failure_summary=summary(failure_summary),
    )


def _verify_locked(cfg: Config, *,
                   record_conflict_receipt: bool = True) -> VerificationReceipt:
    path = receipt_path(cfg)
    main = gitops.main_worktree(cfg.root)
    # A malformed cache is evidence of an unsafe state, not permission to
    # erase it.  Stack preflight below also happens before invalidation so an
    # upstream drift keeps the old receipt available for audit.
    if path.exists():
        read_receipt(path, main)

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in _COMPLETE_STATUSES]
    if unfinished:
        invalidate_receipt(path)
        raise AssentError(
            "plan is not complete; every task must be DONE or SKIP "
            f"({', '.join(unfinished)})")

    target_branch = gitops.require_current_branch(main)
    if recover_expanded_bridge_drift(main):
        print("Recovered an Assent-generated AGENTS.md bridge update in the "
              "target worktree.")
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        raise AssentError(f"target worktree {main} is not clean")
    target_tip = gitops.commit_of(main, target_branch)
    source_branch, source_tip, source_worktree = source_snapshot(cfg, main)
    script = (cfg.assent_dir / "verify.py").resolve()
    if not script.is_file():
        raise AssentError(f"Verification script not found: {script}")
    digest = sha256_file(script)
    upstream_sources = _stack_sources(cfg, target_tip, source_tip)
    # Resolved with the other preflight facts, so a conflicting or unresolvable
    # provisioned link refuses while the previous receipt is still on disk.
    worktrees = [source_worktree,
                 *(source.worktree for source in upstream_sources)]
    # Every contributing live source is classified and its Assent-owned links
    # reconciled before a candidate exists, so this path never depends on an
    # earlier `run` having left a junction behind, and UNKNOWN or STALE refuses
    # here with the zero-AI review remedy rather than at the verifier.
    shared_sources = [(cfg.tasks_name, source_worktree),
                      *((source.plan, source.worktree)
                        for source in upstream_sources)]
    contracts = shared_paths.prepare_sources(main, shared_sources)
    shared_inputs = shared_paths.shared_inputs_digest(main, contracts)
    links = union_worktree_links(worktrees)
    invalidate_receipt(path)

    integration_tree = gitops.tree_of(main, target_tip)
    result: subprocess.CompletedProcess[str] | None = None
    message = f"verify({cfg.tasks_name}): temporary integration candidate"
    with gitops.temporary_integration_worktree(
            main, cfg.tasks_name, target_tip) as (candidate, _branch):
        outcome = gitops.merge_no_ff(candidate, source_tip, message)
        if not outcome.ok:
            conflicts = ", ".join(outcome.conflicts)
            if not record_conflict_receipt:
                raise CandidateConflict(FullVerifyEvidence(
                    "TARGET_CONFLICT", (cfg.tasks_name,), target_tip,
                    (source_tip,), integration_tree, digest, shared_inputs,
                    outcome.exit_code or 1,
                    (f"{_CONFLICT_SUMMARY_PREFIX}{conflicts}",
                     *(f"{cfg.tasks_name}:{path}"
                       for path in outcome.conflicts))))
            receipt = _new_receipt(
                status="FAILED", source_tip=source_tip, target_tip=target_tip,
                integration_tree=integration_tree, digest=digest,
                shared_inputs=shared_inputs,
                exit_code=outcome.exit_code or 1,
                failure_summary=f"{_CONFLICT_SUMMARY_PREFIX}{conflicts}")
        else:
            history = gitops.commit_history(candidate, "HEAD")
            if not history or history[0][1] != (target_tip, source_tip):
                raise AssentError(
                    "temporary integration did not produce the expected two-parent "
                    "candidate")
            integration_tree = gitops.tree_of(candidate, "HEAD")
            # Every source worktree whose commits are in this candidate may
            # hold ignored directory links and generated leaf files; the
            # verifier needs the same ones, and an unmirrorable artifact
            # refuses here rather than producing evidence for a candidate
            # nobody provisioned.
            with provisioned_candidate_links(candidate, links):
                try:
                    result = run_full_verifier(script, candidate)
                except OSError as e:
                    receipt = _new_receipt(
                        status="FAILED", source_tip=source_tip,
                        target_tip=target_tip,
                        integration_tree=integration_tree,
                        digest=digest, shared_inputs=shared_inputs,
                        exit_code=1,
                        failure_summary=f"Unable to start verification: {e}")
                else:
                    receipt = _new_receipt(
                        status="PASSED" if result.returncode == 0 else "FAILED",
                        source_tip=source_tip, target_tip=target_tip,
                        integration_tree=integration_tree, digest=digest,
                        shared_inputs=shared_inputs,
                        exit_code=result.returncode,
                        failure_summary=("" if result.returncode == 0 else summary(
                            result.stdout, result.stderr,
                            f"Verification command failed: {VERIFY_COMMAND} "
                            f"(exit code {result.returncode})",
                            # Appended last so it survives a truncated capture.
                            ignored_input_diagnosis(
                                f"{result.stdout}\n{result.stderr}",
                                worktrees))),
                    )

    # External Git writes are unsupported, but detecting them keeps the receipt
    # from certifying a candidate different from the identities observed above.
    changed: list[str] = []
    if gitops.current_branch(main) != target_branch:
        changed.append("target branch changed")
    elif gitops.commit_of(main, target_branch) != target_tip:
        changed.append("target tip changed")
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        changed.append("target worktree became dirty")
    if gitops.branch_tip(main, source_branch) != source_tip:
        changed.append("source tip changed")
    if source_worktree is not None and not gitops.working_tree_status(
            source_worktree, cfg.git_excludes).is_clean:
        changed.append("source worktree became dirty")
    try:
        current_upstreams = _stack_sources(cfg, target_tip, source_tip)
    except AssentError as e:
        changed.append(f"upstream stack changed: {e}")
    else:
        if current_upstreams != upstream_sources:
            changed.append("upstream source identity changed")
    if sha256_file(script) != digest:
        changed.append("verification script changed")
    # Snapshotted again after the verifier: a declared target whose content moved
    # during the run turns an apparent pass into a failure, so no PASSED receipt
    # can describe inputs the verifier never actually saw.
    # Reclassify against the current manifest after the verifier, rather than
    # hashing the original Contract objects again.  A concurrent review that
    # replaces a profile with a different identity must invalidate the run even
    # when its path list and target bytes happen to be unchanged.
    try:
        if current_shared_inputs(cfg) != shared_inputs:
            changed.append(_SHARED_INPUT_DRIFT)
    except AssentError as e:
        changed.append(f"shared inputs became unreadable: {e}")
    if changed:
        receipt = _new_receipt(
            status="FAILED", source_tip=source_tip, target_tip=target_tip,
            integration_tree=integration_tree, digest=digest,
            shared_inputs=shared_inputs, exit_code=1,
            failure_summary="; ".join(changed))

    write_receipt(path, receipt, main)
    return receipt


def verify_plan_receipt(cfg: Config) -> int:
    """Verify exactly ``cfg.tasks_name`` and return zero only for PASSED."""
    plan_name = cfg.tasks_name
    result = verify_plan_action(cfg, _record_conflict_receipt=True)
    if result.passed:
        print(f"verify {plan_name}: passed ({result.candidate_tree})")
        return 0
    detail = result.evidence[0] if result.evidence else result.outcome
    print(f"verify {plan_name}: failed ({detail})")
    if result.outcome == "TARGET_CONFLICT":
        print(f"Run `assent reconcile {plan_name}` to resolve the source-versus-"
              "target conflict in an isolated worktree, then verify again.")
    return 1


def _receipt_matches_current_candidate_locked(cfg: Config) -> bool:
    """Compare any settled receipt while integration and plan locks are held."""
    main = gitops.main_worktree(cfg.root)
    receipt = read_receipt(receipt_path(cfg), main)
    script = (cfg.assent_dir / "verify.py").resolve()
    if receipt.verify_script_sha256 != sha256_file(script):
        return False
    target_branch = gitops.require_current_branch(main)
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        return False
    target_tip = gitops.commit_of(main, target_branch)
    _source_branch, source_tip, worktree = source_snapshot(cfg, main)
    if source_tip != receipt.source_tip:
        return False
    upstream_sources = _stack_sources(cfg, target_tip, source_tip)
    try:
        current_shared = _current_shared_inputs(
            cfg, main, worktree, upstream_sources)
    except AssentError:
        return False
    if current_shared != receipt.shared_inputs_sha256:
        return False
    tree, outcome = candidate_tree(
        main, cfg.tasks_name, target_tip, source_tip)
    if outcome.ok:
        return tree == receipt.integration_tree
    expected = _CONFLICT_SUMMARY_PREFIX + ", ".join(outcome.conflicts)
    return (receipt.status == "FAILED"
            and receipt.target_tip == target_tip
            and receipt.integration_tree == tree
            and receipt.failure_summary == expected)


def _evidence_from_receipt(cfg: Config, receipt: VerificationReceipt, *,
                           reused: bool) -> FullVerifyEvidence:
    if receipt.status == "PASSED":
        outcome = "PASSED"
    elif receipt.failure_summary.startswith(_CONFLICT_SUMMARY_PREFIX):
        outcome = "TARGET_CONFLICT"
    elif receipt.failure_summary.startswith((
            "Unable to start verification:",
            "target branch changed", "target tip changed",
            "target worktree became dirty", "source tip changed",
            "source worktree became dirty", "upstream stack changed",
            "verification script changed", "shared inputs became unreadable",
            _SHARED_INPUT_DRIFT)):
        outcome = "INFRASTRUCTURE_FAILED"
    else:
        outcome = "VERIFIER_FAILED"
    return FullVerifyEvidence(
        outcome, (cfg.tasks_name,), receipt.target_tip,
        (receipt.source_tip,), receipt.integration_tree,
        receipt.verify_script_sha256, receipt.shared_inputs_sha256,
        receipt.exit_code,
        tuple(item for item in (receipt.failure_summary,) if item), reused)


def verify_plan_action(cfg: Config, *, recheck: bool = False,
                         _record_conflict_receipt: bool = False
                         ) -> FullVerifyEvidence:
    """Run or reuse the exact plan transaction and return typed evidence."""
    plan_name = cfg.tasks_name
    try:
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, plan_name):
                path = receipt_path(cfg)
                if path.exists():
                    receipt = read_receipt(path, gitops.main_worktree(cfg.root))
                    if (_receipt_matches_current_candidate_locked(cfg)
                            and (receipt.status == "PASSED" or not recheck)):
                        print(f"verify {plan_name}: existing {receipt.status} "
                              "receipt matches the current candidate; full "
                              "suite skipped")
                        return _evidence_from_receipt(cfg, receipt, reused=True)
                receipt = _verify_locked(
                    cfg, record_conflict_receipt=_record_conflict_receipt)
                return _evidence_from_receipt(cfg, receipt, reused=False)
    except CandidateConflict as error:
        return error.result
    except (LockBusy, AssentError) as error:
        return FullVerifyEvidence(
            "INFRASTRUCTURE_FAILED", (plan_name,), "", (), "", "", "", 1,
            (str(error),))


def _current_shared_inputs(
        cfg: Config, main: Path, worktree: Path | None,
        upstream_sources: tuple[gitops.PlanSourceSnapshot, ...]) -> str:
    """Recompute this plan's shared-input digest without repairing anything.

    Freshness is a question, not a repair: a profile that changed identity, a
    declared target that moved, or content that differs recomputes to another
    digest and makes the receipt stale.  A source that can no longer be
    classified has no digest at all, which is likewise not the recorded one, so
    acceptance refuses instead of publishing on unproven evidence.
    """
    sources = [(cfg.tasks_name, worktree),
               *((source.plan, source.worktree) for source in upstream_sources)]
    contracts: list[tuple[str, shared_paths.Contract]] = []
    manifest = shared_paths.read_manifest(main)
    for plan_name, tree in sources:
        # A vanished source worktree falls back to the primary worktree, exactly
        # as ``prepare_sources`` did when the receipt was written.
        contract = shared_paths.classify(main, tree or main, manifest)
        if not contract.settled:
            raise AssentError(
                f"the shared-path contract for {plan_name} is {contract.state}; "
                "the receipt's shared-input evidence can no longer be reproduced")
        shared_paths.require_directory_link_agreement(
            main, tree or main, contract, plan_name=plan_name)
        contracts.append((plan_name, contract))
    return shared_paths.shared_inputs_digest(main, contracts)


def current_shared_inputs(cfg: Config) -> str:
    """This plan's shared-input digest as it stands right now.

    ``accept`` uses it for the same pre-publication recheck it already performs
    on the source, target, and verifier: the evidence a receipt was written
    against must still reproduce at the moment a ref is about to move.  It only
    reads and classifies -- it never provisions, repairs, or invokes AI.
    """
    main = gitops.main_worktree(cfg.root)
    target_tip = gitops.commit_of(main, gitops.require_current_branch(main))
    _branch, source_tip, worktree = source_snapshot(cfg, main)
    return _current_shared_inputs(
        cfg, main, worktree, _stack_sources(cfg, target_tip, source_tip))


def receipt_matches_current_candidate(cfg: Config) -> bool:
    """Rebuild the current candidate and compare its exact tree to a PASSED receipt.

    The diagnostic target tip is deliberately not compared.  A new target commit
    with an identical tree remains usable only when rebuilding the merge produces
    the exact receipt tree.
    """
    with hold_integration_lock(cfg.assent_dir):
        with hold_lock(cfg.tasks_dir, cfg.tasks_name):
            return _receipt_matches_current_candidate_locked(cfg)


def verify_plan_receipt_if_needed(cfg: Config) -> int:
    """Run unattended verification unless an exact current PASSED receipt exists.

    This is the post-task scheduler entry point.  It deliberately acquires the
    repository integration lock before the plan lock, after the AI session has
    released its plan lock.  A malformed existing receipt is refused rather
    than silently replaced; explicit ``assent verify`` remains the refresh path.
    """
    plan_name = cfg.tasks_name
    try:
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, plan_name):
                plan = Plan.parse(cfg.tasks_dir)
                if any(task.status not in _COMPLETE_STATUSES for task in plan.tasks):
                    return 0
                path = receipt_path(cfg)
                if path.exists():
                    try:
                        fresh = _receipt_matches_current_candidate_locked(cfg)
                    except AssentError as e:
                        print(f"verify {plan_name}: invalid existing receipt ({e})")
                        return 1
                    if fresh:
                        receipt = read_receipt(path, gitops.main_worktree(cfg.root))
                        print("verify " + plan_name + f": existing {receipt.status} "
                              "receipt matches the current candidate "
                              f"({receipt.integration_tree}); full suite skipped")
                        return 0 if receipt.status == "PASSED" else 1
                    print(f"verify {plan_name}: existing receipt is stale; refreshing")
                receipt = _verify_locked(cfg)
    except LockBusy as e:
        print(f"verify {plan_name}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify {plan_name}: failed ({e})")
        return 1
    if receipt.status == "PASSED":
        print(f"verify {plan_name}: passed ({receipt.integration_tree})")
        return 0
    print(f"verify {plan_name}: failed ({receipt.failure_summary})")
    return 1


def receipt_report_lines(cfg: Config) -> list[str]:
    """Return read-only plan-verification facts for the human report.

    Freshness here is intentionally conservative and side-effect free: exact
    source, target, verifier, and shared-input identities are fresh. Acceptance
    remains responsible for rebuilding and comparing the candidate tree.
    """
    path = receipt_path(cfg)
    if not path.exists():
        return ["Plan verification: NOT RUN (no receipt)"]
    try:
        main = gitops.main_worktree(cfg.root)
        receipt = read_receipt(path, main)
        reasons: list[str] = []
        script = (cfg.assent_dir / "verify.py").resolve()
        if receipt.verify_script_sha256 != sha256_file(script):
            reasons.append("verifier changed")
        target_branch = gitops.require_current_branch(main)
        if gitops.commit_of(main, target_branch) != receipt.target_tip:
            reasons.append("target tip changed")
        _branch, source_tip, worktree = source_snapshot(cfg, main)
        if source_tip != receipt.source_tip:
            reasons.append("source tip changed")
        upstream_sources = _stack_sources(
            cfg, gitops.commit_of(main, target_branch), source_tip)
        if _current_shared_inputs(
                cfg, main, worktree,
                upstream_sources) != receipt.shared_inputs_sha256:
            reasons.append("shared inputs changed")
        if receipt.status != "PASSED":
            reasons.append(f"exit code {receipt.exit_code}")
    except AssentError as e:
        return [f"Plan verification: INVALID ({e})"]

    freshness = "fresh" if not reasons else "stale: " + "; ".join(reasons)
    lines = [
        f"Plan verification: {receipt.status} ({freshness})",
        f"  Source tip: {receipt.source_tip}",
        f"  Candidate tree: {receipt.integration_tree}",
        f"  Completed at: {receipt.completed_at}",
    ]
    if receipt.failure_summary:
        lines.append("  Failure: " + receipt.failure_summary.splitlines()[0])
    return lines
