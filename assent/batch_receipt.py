"""The batch receipt: one full verification covering several folders.

One candidate tree merges every queued folder in a recorded order, is verified
once, and is then released folder by folder.  Each intermediate merge commit
must be comparable against the receipt, so the receipt stores the tree after
every step, not only the final tree.

The batch receipt (``.assent/_batch_verification.toml``) spans folders and
therefore lives in ``.assent/`` itself.  It never reads, writes, or depends on
the per-folder receipt in ``assent.folder_verification``: the two evidence
models sit side by side and stay independent.

This module owns the evidence only -- its data, bytes, and freshness rules.
Building and verifying a batch candidate is ``assent.batch_verification``.
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from assent import AssentError, gitops
from assent.config import Config, validate_tasks_name
from assent.verification_common import (DIGEST_RE, RECEIPT_STATUSES,
                                        SUMMARY_LIMIT, VERIFY_COMMAND,
                                        atomic_write_text,
                                        build_batch_candidate,
                                        invalidate_receipt, require_oid,
                                        toml_string, verifier_digest)

BATCH_RECEIPT_NAME = "_batch_verification.toml"
BATCH_RECEIPT_VERSION = 1
_BATCH_RECEIPT_KEYS = {
    "version", "status", "target_tip", "sources", "final_tree",
    "verify_script_sha256", "verify_command", "exit_code", "completed_at",
    "failure_summary",
}
_BATCH_SOURCE_KEYS = {"folder", "source_tip", "step_tree"}


@dataclass(frozen=True)
class BatchSource:
    """One folder's place in the recorded merge order.

    ``step_tree`` is the candidate tree right after this folder was merged, so a
    release can compare every intermediate merge commit it creates, not just the
    end of the chain.
    """

    folder: str
    source_tip: str
    step_tree: str


@dataclass(frozen=True)
class BatchVerificationReceipt:
    """Evidence of one full verification covering an ordered list of folders."""

    version: int
    status: str
    target_tip: str
    sources: tuple[BatchSource, ...]
    final_tree: str
    verify_script_sha256: str
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
    def folders(self) -> tuple[str, ...]:
        """Folder names in the recorded merge order."""
        return tuple(source.folder for source in self.sources)


def batch_receipt_path(assent_dir: str | Path) -> Path:
    """Return the repository-level batch receipt path.

    The batch receipt spans folders, so it belongs to ``.assent/`` itself and
    never to one folder's directory.
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
        f"verify_command = {toml_string(receipt.verify_command)}\n"
        f"exit_code = {receipt.exit_code}\n"
        f"completed_at = {toml_string(receipt.completed_at)}\n"
        f"failure_summary = {toml_string(receipt.failure_summary)}\n"
    )
    for source in receipt.sources:
        text += (
            "\n[[sources]]\n"
            f"folder = {toml_string(source.folder)}\n"
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
        if not isinstance(source.folder, str):
            raise AssentError(
                f"Batch verification receipt sources[{index}] folder must be a string")
        validate_tasks_name(source.folder, "Batch verification receipt folder")
        if source.folder in seen:
            raise AssentError(
                "Batch verification receipt lists folder "
                f"{source.folder} more than once")
        seen.add(source.folder)
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
            branch = gitops.unique_folder_branch(main, source.folder)
        except AssentError as e:
            reasons.append(f"source branch for {source.folder} is ambiguous: {e}")
            continue
        if branch is None:
            reasons.append(f"source branch for {source.folder} no longer exists")
            continue
        tip = gitops.branch_tip(main, branch)
        if tip != source.source_tip:
            reasons.append(
                f"source tip for {source.folder} changed from {source.source_tip} "
                f"to {tip}")
            continue
        if gitops.is_ancestor(main, tip, target_tip):
            reasons.append(
                f"{source.folder} has already been accepted into the target "
                "on its own")
    if reasons:
        return tuple(reasons)

    candidate = build_batch_candidate(
        main, target_tip,
        [(source.folder, source.source_tip) for source in receipt.sources])
    if not candidate.ok:
        return (f"rebuilt integration of {candidate.conflict_folder} conflicts: "
                + ", ".join(candidate.conflicts),)
    for source, tree in zip(receipt.sources, candidate.step_trees):
        if tree != source.step_tree:
            reasons.append(
                f"rebuilt step tree for {source.folder} is {tree}, not the "
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
