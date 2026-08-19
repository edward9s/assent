"""The batch receipt: one full verification covering several plans.

One candidate tree merges every queued plan in a recorded order, is verified
once, and is then released plan by plan.  Each intermediate merge commit
must be comparable against the receipt, so the receipt stores the tree after
every step, not only the final tree.

The batch receipt (``.assent/_batch_verification.toml``) spans plans and
therefore lives in ``.assent/`` itself.  It never reads, writes, or depends on
the per-plan receipt in ``assent.plan_verification``: the two evidence
models sit side by side and stay independent.

This module owns the evidence only -- its data, bytes, and freshness rules.
Building and verifying a batch candidate is ``assent.batch_verification``.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from assent import AssentError, gitops, shared_paths
from assent.config import Config, validate_tasks_name
from assent.verification_common import (DIGEST_RE, RECEIPT_STATUSES,
                                        SUMMARY_LIMIT, VERIFY_COMMAND,
                                        atomic_write_text,
                                        build_batch_candidate,
                                        invalidate_receipt, require_oid,
                                        toml_string, verifier_digest)

BATCH_RECEIPT_NAME = "_batch_verification.toml"
# 2 adds shared_inputs_sha256.  The bump is fail-closed on purpose: a version-1
# receipt recorded no shared-input evidence at all, so it is stale and unusable
# rather than silently upgraded with an assumed-empty digest.
BATCH_RECEIPT_VERSION = 2
_BATCH_RECEIPT_KEYS = {
    "version", "status", "target_tip", "sources", "final_tree",
    "verify_script_sha256", "shared_inputs_sha256", "verify_command",
    "exit_code", "completed_at", "failure_summary",
}
_BATCH_SOURCE_KEYS = {"plan", "source_tip", "step_tree"}


@dataclass(frozen=True)
class BatchSource:
    """One plan's place in the recorded merge order.

    ``step_tree`` is the candidate tree right after this plan was merged, so a
    release can compare every intermediate merge commit it creates, not just the
    end of the chain.
    """

    plan: str
    source_tip: str
    step_tree: str


@dataclass(frozen=True)
class BatchVerificationReceipt:
    """Evidence of one full verification covering an ordered list of plans."""

    version: int
    status: str
    target_tip: str
    sources: tuple[BatchSource, ...]
    final_tree: str
    verify_script_sha256: str
    #: Digest of every reviewed shared input this batch verification depended
    #: on -- the selected profiles and the exact content of their targets.
    shared_inputs_sha256: str
    verify_command: str
    exit_code: int
    completed_at: str
    failure_summary: str

    def __post_init__(self) -> None:
        # A TOML array reads back as a list; normalizing keeps a receipt equal to
        # its own round trip without coercing the element type, which stays a
        # validated schema error.
        if isinstance(self.sources, list):
            object.__setattr__(self, "sources", tuple(self.sources))

    @property
    def plan_names(self) -> tuple[str, ...]:
        """Plan names in the recorded merge order."""
        return tuple(source.plan for source in self.sources)


def batch_receipt_path(assent_dir: str | Path) -> Path:
    """Return the repository-level batch receipt path.

    The batch receipt spans plans, so it belongs to ``.assent/`` itself and
    never to one plan's directory.
    """
    return Path(assent_dir) / BATCH_RECEIPT_NAME


def invalidate_batch_receipt(assent_dir: str | Path) -> bool:
    """Delete the batch receipt so no release can consume it; True if one existed.

    This is the single invalidation entry point for every command that changes
    what a batch was verified against: ``reject`` and ``rework`` reopen work the
    candidate was built from, and a batch release consumes the receipt once it
    has published.  The receipt is derived and disposable, so deleting it only
    ever costs one ``assent verify --batch``; it never destroys a source of
    truth.
    """
    path = batch_receipt_path(assent_dir)
    existed = path.exists()
    invalidate_receipt(path)
    return existed


def _batch_receipt_text(receipt: BatchVerificationReceipt) -> str:
    text = (
        f"version = {receipt.version}\n"
        f"status = {toml_string(receipt.status)}\n"
        f"target_tip = {toml_string(receipt.target_tip)}\n"
        f"final_tree = {toml_string(receipt.final_tree)}\n"
        f"verify_script_sha256 = {toml_string(receipt.verify_script_sha256)}\n"
        f"shared_inputs_sha256 = {toml_string(receipt.shared_inputs_sha256)}\n"
        f"verify_command = {toml_string(receipt.verify_command)}\n"
        f"exit_code = {receipt.exit_code}\n"
        f"completed_at = {toml_string(receipt.completed_at)}\n"
        f"failure_summary = {toml_string(receipt.failure_summary)}\n"
    )
    for source in receipt.sources:
        text += (
            "\n[[sources]]\n"
            f"plan = {toml_string(source.plan)}\n"
            f"source_tip = {toml_string(source.source_tip)}\n"
            f"step_tree = {toml_string(source.step_tree)}\n"
        )
    return text


def _require_oid(value: object, name: str) -> None:
    require_oid(value, name, "Batch verification receipt")


def _validate_batch_receipt(receipt: BatchVerificationReceipt,
                            repository: Path) -> None:
    """Fail closed on any incomplete, inconsistent, or non-existent identity."""
    if type(receipt.version) is not int or receipt.version != BATCH_RECEIPT_VERSION:
        raise AssentError(
            f"Batch verification receipt version must be {BATCH_RECEIPT_VERSION}")
    if receipt.status not in RECEIPT_STATUSES:
        raise AssentError(
            "Batch verification receipt status must be PASSED or FAILED")
    _require_oid(receipt.target_tip, "target_tip")
    _require_oid(receipt.final_tree, "final_tree")
    if not isinstance(receipt.sources, tuple) or not receipt.sources:
        raise AssentError(
            "Batch verification receipt sources must be a non-empty ordered list")
    seen: set[str] = set()
    for index, source in enumerate(receipt.sources):
        if not isinstance(source, BatchSource):
            raise AssentError(
                f"Batch verification receipt sources[{index}] is not a source entry")
        if not isinstance(source.plan, str):
            raise AssentError(
                f"Batch verification receipt sources[{index}] plan must be a string")
        validate_tasks_name(source.plan, "Batch verification receipt plan")
        if source.plan in seen:
            raise AssentError(
                "Batch verification receipt lists plan "
                f"{source.plan} more than once")
        seen.add(source.plan)
        _require_oid(source.source_tip, f"sources[{index}] source_tip")
        _require_oid(source.step_tree, f"sources[{index}] step_tree")
    if receipt.final_tree != receipt.sources[-1].step_tree:
        raise AssentError(
            "Batch verification receipt final_tree must equal the last step_tree")
    if not isinstance(receipt.verify_script_sha256, str) or not DIGEST_RE.fullmatch(
            receipt.verify_script_sha256):
        raise AssentError(
            "Batch verification receipt verify_script_sha256 must be a "
            "64-character lowercase hexadecimal digest")
    if not isinstance(receipt.shared_inputs_sha256, str) or not DIGEST_RE.fullmatch(
            receipt.shared_inputs_sha256):
        raise AssentError(
            "Batch verification receipt shared_inputs_sha256 must be a "
            "64-character lowercase hexadecimal digest")
    if receipt.verify_command != VERIFY_COMMAND:
        raise AssentError(
            "Batch verification receipt verify_command must be "
            f"{VERIFY_COMMAND!r}")
    if type(receipt.exit_code) is not int:
        raise AssentError(
            "Batch verification receipt exit_code must be an integer")
    if not isinstance(receipt.completed_at, str):
        raise AssentError(
            "Batch verification receipt completed_at must be a string")
    try:
        completed = datetime.fromisoformat(receipt.completed_at)
    except ValueError as e:
        raise AssentError(
            "Batch verification receipt completed_at must be ISO 8601") from e
    if completed.tzinfo is None:
        raise AssentError(
            "Batch verification receipt completed_at must include a timezone")
    if not isinstance(receipt.failure_summary, str):
        raise AssentError(
            "Batch verification receipt failure_summary must be a string")
    if len(receipt.failure_summary) > SUMMARY_LIMIT:
        raise AssentError(
            "Batch verification receipt failure_summary exceeds the size limit")
    if receipt.status == "PASSED" and receipt.exit_code != 0:
        raise AssentError(
            "A PASSED batch verification receipt must have exit_code = 0")
    if receipt.status == "FAILED" and receipt.exit_code == 0:
        raise AssentError(
            "A FAILED batch verification receipt must have a nonzero exit_code")
    expected = [(receipt.target_tip, "commit"), (receipt.final_tree, "tree")]
    for source in receipt.sources:
        expected.append((source.source_tip, "commit"))
        expected.append((source.step_tree, "tree"))
    for object_id, wanted in expected:
        actual = gitops.object_type(repository, object_id)
        if actual != wanted:
            raise AssentError(
                f"Batch verification receipt {object_id} names a Git {actual}, "
                f"not a {wanted}")


def write_batch_receipt(path: Path, receipt: BatchVerificationReceipt,
                        repository: Path) -> None:
    """Atomically replace the batch receipt after validating schema and objects."""
    path = Path(path)
    _validate_batch_receipt(receipt, repository)
    atomic_write_text(path, _batch_receipt_text(receipt))


def read_batch_receipt(path: Path,
                       repository: Path) -> BatchVerificationReceipt:
    """Read the batch receipt fail-closed, including Git existence and types."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as e:
        raise AssentError(f"Batch verification receipt not found: {path}") from e
    except OSError as e:
        raise AssentError(
            f"Unable to read batch verification receipt {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AssentError(
            f"Batch verification receipt is not valid TOML ({path}): {e}") from e
    if not isinstance(data, dict):
        raise AssentError("Batch verification receipt must be a TOML table")
    unknown = sorted(set(data) - _BATCH_RECEIPT_KEYS)
    missing = sorted(_BATCH_RECEIPT_KEYS - set(data))
    if unknown:
        raise AssentError(
            f"Batch verification receipt has unknown keys: {', '.join(unknown)}")
    if missing:
        raise AssentError(
            f"Batch verification receipt is missing keys: {', '.join(missing)}")
    raw_sources = data.pop("sources")
    if not isinstance(raw_sources, list):
        raise AssentError(
            "Batch verification receipt sources must be an array of tables")
    sources: list[BatchSource] = []
    for index, item in enumerate(raw_sources):
        if not isinstance(item, dict):
            raise AssentError(
                f"Batch verification receipt sources[{index}] is not a table")
        unknown = sorted(set(item) - _BATCH_SOURCE_KEYS)
        missing = sorted(_BATCH_SOURCE_KEYS - set(item))
        if unknown:
            raise AssentError(
                f"Batch verification receipt sources[{index}] has unknown keys: "
                f"{', '.join(unknown)}")
        if missing:
            raise AssentError(
                f"Batch verification receipt sources[{index}] is missing keys: "
                f"{', '.join(missing)}")
        sources.append(BatchSource(**item))
    try:
        receipt = BatchVerificationReceipt(sources=tuple(sources), **data)
    except TypeError as e:
        raise AssentError(
            f"Batch verification receipt has an invalid structure: {e}") from e
    _validate_batch_receipt(receipt, repository)
    return receipt


def current_batch_shared_inputs(main: Path,
                                receipt: BatchVerificationReceipt) -> str:
    """Recompute the batch's shared-input digest without repairing anything.

    Freshness is a question, not a repair: a profile that changed identity, a
    declared target that moved, or target content that differs recomputes to
    another digest, and a source that can no longer be classified has no digest
    at all.  Both outcomes leave the receipt stale, which is what acceptance
    needs -- it may never provision a link as a side effect of publishing.
    """
    manifest = shared_paths.read_manifest(main)
    contracts: list[tuple[str, shared_paths.Contract]] = []
    for source in receipt.sources:
        worktree = gitops.plan_worktree(main, source.plan)
        contract = shared_paths.classify(main, worktree or main, manifest)
        if not contract.settled:
            raise AssentError(
                f"the shared-path contract for {source.plan} is "
                f"{contract.state}; the batch receipt's shared-input evidence "
                "can no longer be reproduced")
        shared_paths.require_directory_link_agreement(
            main, worktree or main, contract, plan_name=source.plan)
        contracts.append((source.plan, contract))
    return shared_paths.shared_inputs_digest(main, contracts)


def batch_receipt_staleness(cfg: Config,
                            receipt: BatchVerificationReceipt) -> tuple[str, ...]:
    """Return every reason the whole batch has expired; empty means still current.

    A batch receipt is all-or-nothing: any one drifted source expires the batch,
    because the recorded merge order and its step trees only describe that exact
    set of sources.  Cheap identity checks run first; the merge chain is rebuilt
    only when every recorded identity is still current, and the target tip itself
    is deliberately not compared -- a target that moved without changing the
    rebuilt trees keeps the receipt usable.
    """
    main = gitops.main_worktree(cfg.root)
    reasons: list[str] = []
    try:
        if verifier_digest(cfg) != receipt.verify_script_sha256:
            reasons.append("verification script changed")
    except AssentError as e:
        reasons.append(f"verification script is unavailable: {e}")

    target_tip = gitops.commit_of(main, gitops.require_current_branch(main))
    for source in receipt.sources:
        try:
            branch = gitops.unique_plan_branch(main, source.plan)
        except AssentError as e:
            reasons.append(f"source branch for {source.plan} is ambiguous: {e}")
            continue
        if branch is None:
            reasons.append(f"source branch for {source.plan} no longer exists")
            continue
        tip = gitops.branch_tip(main, branch)
        if tip != source.source_tip:
            reasons.append(
                f"source tip for {source.plan} changed from {source.source_tip} "
                f"to {tip}")
            continue
        if gitops.is_ancestor(main, tip, target_tip):
            reasons.append(
                f"{source.plan} has already been accepted into the target "
                "on its own")
    if reasons:
        return tuple(reasons)

    # Only once every recorded source identity is still current does asking
    # about the shared inputs mean anything: a vanished source is already
    # reported above and would otherwise be reported twice.
    try:
        if current_batch_shared_inputs(
                main, receipt) != receipt.shared_inputs_sha256:
            return ("the reviewed shared inputs changed since verification",)
    except AssentError as e:
        return (f"the reviewed shared inputs cannot be reproduced: {e}",)

    candidate = build_batch_candidate(
        main, target_tip,
        [(source.plan, source.source_tip) for source in receipt.sources])
    if not candidate.ok:
        return (f"rebuilt integration of {candidate.conflict_plan} conflicts: "
                + ", ".join(candidate.conflicts),)
    for source, tree in zip(receipt.sources, candidate.step_trees):
        if tree != source.step_tree:
            reasons.append(
                f"rebuilt step tree for {source.plan} is {tree}, not the "
                f"recorded {source.step_tree}")
            break
    if not reasons and candidate.step_trees[-1] != receipt.final_tree:
        reasons.append(
            f"rebuilt final tree is {candidate.step_trees[-1]}, not the recorded "
            f"{receipt.final_tree}")
    return tuple(reasons)


def batch_receipt_is_current(cfg: Config,
                             receipt: BatchVerificationReceipt) -> bool:
    """True only for a PASSED batch receipt with no staleness reason left."""
    if receipt.status != "PASSED":
        return False
    return not batch_receipt_staleness(cfg, receipt)
