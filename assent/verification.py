"""Reproducible, unattended full verification of an integration candidate.

The receipts written here are a derived runtime cache.  They record facts about
explicit source snapshots, the resulting integration trees, and the main-tree
verification script; they are not human approval and never advance a Git ref.

Two independent receipt models live side by side and never read each other's
files:

* the per-folder receipt (``<folder>/_verification.toml``), covering one folder
  merged into the target;
* the batch receipt (``.assent/_batch_verification.toml``), covering one full
  verification of several folders merged in a recorded order, so that a batch
  release can publish them one by one against a single test run.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError, gitops
from assent.config import Config, _validate_tasks_name, load_config
from assent.folderdeps import (infer_folder_completion, live_upstreams,
                               order_folders_by_dependency,
                               parse_folder_dependencies,
                               parse_folder_dependency_graph)
from assent.lockfile import LockBusy, hold_integration_lock, hold_lock
from assent.plan import Plan

RECEIPT_NAME = "_verification.toml"
RECEIPT_VERSION = 1
VERIFY_COMMAND = "python .assent/verify.py"
_COMPLETE_STATUSES = ("DONE", "SKIP")
_RECEIPT_STATUSES = ("PASSED", "FAILED")
_OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SUMMARY_LIMIT = 4000
# The receipt records a source/target conflict with this prefix, which is also
# what tells a failed `verify FOLDER` to point at `assent reconcile`.
_CONFLICT_SUMMARY_PREFIX = "Integration conflict: "
_RECEIPT_KEYS = {
    "version", "status", "source_tip", "target_tip", "integration_tree",
    "verify_script_sha256", "verify_command", "exit_code", "completed_at",
    "failure_summary",
}


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


def receipt_path(cfg: Config) -> Path:
    """Return the explicitly selected folder's derived receipt path."""
    return cfg.tasks_dir / RECEIPT_NAME


def _invalidate_receipt(path: Path) -> None:
    """Remove stale derived evidence before starting a replacement run."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        raise AssentError(
            f"Unable to invalidate old verification receipt {path}: {e}") from e


def invalidate_folder_receipt(cfg: Config) -> bool:
    """Delete one folder's receipt so no accept can consume it; True if one existed.

    Every command that changes what the receipt was written against uses this
    rather than unlinking the file itself.  The receipt is derived and
    disposable, so deleting it only ever costs one ``assent verify FOLDER``.
    """
    path = receipt_path(cfg)
    existed = path.exists()
    _invalidate_receipt(path)
    return existed


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as e:
        raise AssentError(f"Unable to read verification script {path}: {e}") from e
    return digest.hexdigest()


def verifier_digest(cfg: Config) -> str:
    """Return the current main-tree verifier digest used by receipt gates."""
    script = (cfg.assent_dir / "verify.py").resolve()
    if not script.is_file():
        raise AssentError(f"Verification script not found: {script}")
    return _sha256(script)


def _summary(*parts: str) -> str:
    """Normalize child diagnostics and bound receipt growth."""
    text = "\n".join(part.strip() for part in parts if part and part.strip())
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character if (character in "\n\t" or ord(character) >= 0x20)
        and character != "\ufffd" else "?"
        for character in text
    )
    if len(text) > _SUMMARY_LIMIT:
        marker = "...[earlier output truncated]\n"
        text = marker + text[-(_SUMMARY_LIMIT - len(marker)):]
    return text


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _receipt_text(receipt: VerificationReceipt) -> str:
    return (
        f"version = {receipt.version}\n"
        f"status = {_toml_string(receipt.status)}\n"
        f"source_tip = {_toml_string(receipt.source_tip)}\n"
        f"target_tip = {_toml_string(receipt.target_tip)}\n"
        f"integration_tree = {_toml_string(receipt.integration_tree)}\n"
        f"verify_script_sha256 = {_toml_string(receipt.verify_script_sha256)}\n"
        f"verify_command = {_toml_string(receipt.verify_command)}\n"
        f"exit_code = {receipt.exit_code}\n"
        f"completed_at = {_toml_string(receipt.completed_at)}\n"
        f"failure_summary = {_toml_string(receipt.failure_summary)}\n"
    )


def _validate_receipt(receipt: VerificationReceipt, repository: Path) -> None:
    if type(receipt.version) is not int or receipt.version != RECEIPT_VERSION:
        raise AssentError(
            f"Verification receipt version must be {RECEIPT_VERSION}")
    if receipt.status not in _RECEIPT_STATUSES:
        raise AssentError("Verification receipt status must be PASSED or FAILED")
    for name in ("source_tip", "target_tip", "integration_tree"):
        value = getattr(receipt, name)
        if not isinstance(value, str) or not _OID_RE.fullmatch(value):
            raise AssentError(
                f"Verification receipt {name} must be a 40- or 64-character "
                "lowercase hexadecimal object id")
    if not isinstance(receipt.verify_script_sha256, str) or not _DIGEST_RE.fullmatch(
            receipt.verify_script_sha256):
        raise AssentError(
            "Verification receipt verify_script_sha256 must be a 64-character "
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
    if len(receipt.failure_summary) > _SUMMARY_LIMIT:
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


def _atomic_write_text(path: Path, text: str) -> None:
    """Replace one receipt file in place, flushed and without a partial state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as e:
        raise AssentError(f"Unable to atomically write receipt {path}: {e}") from e
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def write_receipt(path: Path, receipt: VerificationReceipt,
                  repository: Path) -> None:
    """Atomically replace a receipt after validating its schema and Git objects."""
    path = Path(path)
    _validate_receipt(receipt, repository)
    _atomic_write_text(path, _receipt_text(receipt))


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


def _source_snapshot(cfg: Config, main: Path) -> tuple[str, str, Path | None]:
    folder = cfg.tasks_name
    worktree = gitops.folder_worktree(main, folder)
    if worktree is not None:
        branch = gitops.current_branch(worktree)
        if not branch:
            raise AssentError(
                f"source worktree {worktree} is detached; no source branch is explicit")
        if not branch.startswith(f"{folder}/") or branch == f"{folder}/":
            raise AssentError(
                f"source worktree {worktree} is on {branch}, not a {folder}/* branch")
        if not gitops.working_tree_status(
                worktree, cfg.git_excludes).is_clean:
            raise AssentError(f"source worktree {worktree} is not clean")
    else:
        branches = gitops.folder_branches(main, folder)
        if len(branches) != 1:
            detail = ", ".join(branches) if branches else "none"
            raise AssentError(
                f"source branch identity is ambiguous for {folder} ({detail})")
        branch = branches[0]
    return branch, gitops.branch_tip(main, branch), worktree


def _stack_sources(cfg: Config, target_tip: str,
                   downstream_tip: str) -> tuple[gitops.FolderSourceSnapshot, ...]:
    """Snapshot the declared live base and prove the downstream contains it.

    Only direct dependencies participate here.  An unrelated malformed folder
    must not invalidate this folder's receipt, while every declared live
    upstream is required to be complete.  Only the explicitly declared base
    contributes a source identity or ancestry requirement; non-base ``after``
    entries provide ordering only.

    An archived upstream is proven complete by the roster and has no source
    left to snapshot, so it is filtered out first (see ``live_upstreams``).
    """
    dependencies = parse_folder_dependencies(cfg.tasks_dir)
    sources: list[gitops.FolderSourceSnapshot] = []
    for folder in live_upstreams(cfg.assent_dir, dependencies):
        completion = infer_folder_completion(cfg.assent_dir / folder)
        if not completion.complete:
            raise AssentError(
                f"upstream folder {folder} is incomplete: {completion.reason}")
        if dependencies.base != folder:
            continue
        source = gitops.resolve_folder_source(
            cfg.root, folder, cfg.git_excludes)
        sources.append(source)
        if not gitops.is_ancestor(cfg.root, source.tip, downstream_tip):
            raise AssentError(
                f"stale stack for {cfg.tasks_name}: current upstream {folder} tip "
                f"{source.tip} is not an ancestor of downstream tip "
                f"{downstream_tip}; the downstream source and existing receipt "
                f"were preserved. Run `assent rework {cfg.tasks_name}` after "
                "deciding how to handle the upstream change, or replan the "
                "dependency")
    return tuple(sources)


def _new_receipt(*, status: str, source_tip: str, target_tip: str,
                 integration_tree: str, digest: str, exit_code: int,
                 failure_summary: str = "") -> VerificationReceipt:
    return VerificationReceipt(
        version=RECEIPT_VERSION,
        status=status,
        source_tip=source_tip,
        target_tip=target_tip,
        integration_tree=integration_tree,
        verify_script_sha256=digest,
        verify_command=VERIFY_COMMAND,
        exit_code=exit_code,
        completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        failure_summary=_summary(failure_summary),
    )


def _candidate_tree(main: Path, folder: str, target_tip: str,
                    source_tip: str) -> tuple[str | None, gitops.MergeOutcome]:
    """Build and remove one no-FF candidate, returning its tree or conflicts."""
    message = f"verify({folder}): temporary integration candidate"
    with gitops.temporary_integration_worktree(
            main, folder, target_tip) as (candidate, _branch):
        outcome = gitops.merge_no_ff(candidate, source_tip, message)
        if not outcome.ok:
            return None, outcome
        history = gitops.commit_history(candidate, "HEAD")
        if not history or history[0][1] != (target_tip, source_tip):
            raise AssentError(
                "temporary integration did not produce the expected two-parent "
                "candidate")
        return gitops.tree_of(candidate, "HEAD"), outcome


def _run_full_verifier(script: Path,
                       candidate: Path) -> subprocess.CompletedProcess[str]:
    """Run the full suite in the foreground, without an arbitrary timeout.

    The child deliberately remains in Assent's foreground process group.  A
    real Ctrl-C therefore reaches the verifier and the unittest descendants it
    starts, while ``subprocess.run`` waits for the child before the surrounding
    temporary-worktree context removes its resources.
    """
    started = time.monotonic()
    print(f"Full verification started: {VERIFY_COMMAND}", flush=True)
    try:
        result = subprocess.run(
            [sys.executable, str(script)], cwd=str(candidate),
            capture_output=True, encoding="utf-8", errors="replace")
    except KeyboardInterrupt:
        elapsed = time.monotonic() - started
        print("Full verification interrupted: "
              f"elapsed {elapsed:.1f}s, exit code 130", flush=True)
        raise
    except OSError:
        elapsed = time.monotonic() - started
        print("Full verification finished: "
              f"elapsed {elapsed:.1f}s, exit code 1", flush=True)
        raise
    elapsed = time.monotonic() - started
    print("Full verification finished: "
          f"elapsed {elapsed:.1f}s, exit code {result.returncode}",
          flush=True)
    return result


def _verify_locked(cfg: Config) -> VerificationReceipt:
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
        _invalidate_receipt(path)
        raise AssentError(
            "folder is not complete; every task must be DONE or SKIP "
            f"({', '.join(unfinished)})")

    target_branch = gitops.require_current_branch(main)
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        raise AssentError(f"target worktree {main} is not clean")
    target_tip = gitops.commit_of(main, target_branch)
    source_branch, source_tip, source_worktree = _source_snapshot(cfg, main)
    script = (cfg.assent_dir / "verify.py").resolve()
    if not script.is_file():
        raise AssentError(f"Verification script not found: {script}")
    digest = _sha256(script)
    upstream_sources = _stack_sources(cfg, target_tip, source_tip)
    _invalidate_receipt(path)

    candidate_tree = gitops.tree_of(main, target_tip)
    result: subprocess.CompletedProcess[str] | None = None
    message = f"verify({cfg.tasks_name}): temporary integration candidate"
    with gitops.temporary_integration_worktree(
            main, cfg.tasks_name, target_tip) as (candidate, _branch):
        outcome = gitops.merge_no_ff(candidate, source_tip, message)
        if not outcome.ok:
            conflicts = ", ".join(outcome.conflicts)
            receipt = _new_receipt(
                status="FAILED", source_tip=source_tip, target_tip=target_tip,
                integration_tree=candidate_tree, digest=digest,
                exit_code=outcome.exit_code or 1,
                failure_summary=f"{_CONFLICT_SUMMARY_PREFIX}{conflicts}")
        else:
            history = gitops.commit_history(candidate, "HEAD")
            if not history or history[0][1] != (target_tip, source_tip):
                raise AssentError(
                    "temporary integration did not produce the expected two-parent "
                    "candidate")
            candidate_tree = gitops.tree_of(candidate, "HEAD")
            try:
                result = _run_full_verifier(script, candidate)
            except OSError as e:
                receipt = _new_receipt(
                    status="FAILED", source_tip=source_tip,
                    target_tip=target_tip, integration_tree=candidate_tree,
                    digest=digest, exit_code=1,
                    failure_summary=f"Unable to start verification: {e}")
            else:
                receipt = _new_receipt(
                    status="PASSED" if result.returncode == 0 else "FAILED",
                    source_tip=source_tip, target_tip=target_tip,
                    integration_tree=candidate_tree, digest=digest,
                    exit_code=result.returncode,
                    failure_summary=("" if result.returncode == 0 else _summary(
                        result.stdout, result.stderr,
                        f"Verification command failed: {VERIFY_COMMAND} "
                        f"(exit code {result.returncode})")),
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
    if _sha256(script) != digest:
        changed.append("verification script changed")
    if changed:
        receipt = _new_receipt(
            status="FAILED", source_tip=source_tip, target_tip=target_tip,
            integration_tree=candidate_tree, digest=digest, exit_code=1,
            failure_summary="; ".join(changed))

    write_receipt(path, receipt, main)
    return receipt


def verify_folder(cfg: Config) -> int:
    """Verify exactly ``cfg.tasks_name`` and return zero only for PASSED."""
    folder = cfg.tasks_name
    try:
        # Repository lock first, then the selected folder lock, matching accept.
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, folder):
                receipt = _verify_locked(cfg)
    except LockBusy as e:
        print(f"verify {folder}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify {folder}: failed ({e})")
        return 1
    if receipt.status == "PASSED":
        print(f"verify {folder}: passed ({receipt.integration_tree})")
        return 0
    print(f"verify {folder}: failed ({receipt.failure_summary})")
    if receipt.failure_summary.startswith(_CONFLICT_SUMMARY_PREFIX):
        print(f"Run `assent reconcile {folder}` to resolve the source-versus-"
              "target conflict in an isolated worktree, then verify again.")
    return 1


def _receipt_matches_current_candidate_locked(cfg: Config) -> bool:
    """Compare a PASSED receipt while the integration and folder locks are held."""
    main = gitops.main_worktree(cfg.root)
    receipt = read_receipt(receipt_path(cfg), main)
    if receipt.status != "PASSED":
        return False
    script = (cfg.assent_dir / "verify.py").resolve()
    if receipt.verify_script_sha256 != _sha256(script):
        return False
    target_branch = gitops.require_current_branch(main)
    if not gitops.working_tree_status(main, cfg.git_excludes).is_clean:
        return False
    target_tip = gitops.commit_of(main, target_branch)
    _source_branch, source_tip, _worktree = _source_snapshot(cfg, main)
    if source_tip != receipt.source_tip:
        return False
    _stack_sources(cfg, target_tip, source_tip)
    tree, outcome = _candidate_tree(
        main, cfg.tasks_name, target_tip, source_tip)
    return outcome.ok and tree == receipt.integration_tree


def receipt_matches_current_candidate(cfg: Config) -> bool:
    """Rebuild the current candidate and compare its exact tree to a PASSED receipt.

    The diagnostic target tip is deliberately not compared.  A new target commit
    with an identical tree remains usable only when rebuilding the merge produces
    the exact receipt tree.
    """
    with hold_integration_lock(cfg.assent_dir):
        with hold_lock(cfg.tasks_dir, cfg.tasks_name):
            return _receipt_matches_current_candidate_locked(cfg)


def verify_folder_if_needed(cfg: Config) -> int:
    """Run unattended verification unless an exact current PASSED receipt exists.

    This is the post-task scheduler entry point.  It deliberately acquires the
    repository integration lock before the folder lock, after the AI session has
    released its folder lock.  A malformed existing receipt is refused rather
    than silently replaced; explicit ``assent verify`` remains the refresh path.
    """
    folder = cfg.tasks_name
    try:
        with hold_integration_lock(cfg.assent_dir):
            with hold_lock(cfg.tasks_dir, folder):
                plan = Plan.parse(cfg.tasks_dir)
                if any(task.status not in _COMPLETE_STATUSES for task in plan.tasks):
                    return 0
                path = receipt_path(cfg)
                if path.exists():
                    try:
                        fresh = _receipt_matches_current_candidate_locked(cfg)
                    except AssentError as e:
                        print(f"verify {folder}: invalid existing receipt ({e})")
                        return 1
                    if fresh:
                        receipt = read_receipt(path, gitops.main_worktree(cfg.root))
                        print("verify " + folder + ": existing PASSED receipt is "
                              f"fresh ({receipt.integration_tree}); full suite skipped")
                        return 0
                    print(f"verify {folder}: existing receipt is stale; refreshing")
                receipt = _verify_locked(cfg)
    except LockBusy as e:
        print(f"verify {folder}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"verify {folder}: failed ({e})")
        return 1
    if receipt.status == "PASSED":
        print(f"verify {folder}: passed ({receipt.integration_tree})")
        return 0
    print(f"verify {folder}: failed ({receipt.failure_summary})")
    return 1


def receipt_report_lines(cfg: Config) -> list[str]:
    """Return read-only folder-verification facts for the human report.

    Freshness here is intentionally conservative and side-effect free: exact
    source, target, and verifier identities are fresh.  Acceptance remains
    responsible for rebuilding and comparing the candidate tree.
    """
    path = receipt_path(cfg)
    if not path.exists():
        return ["Folder verification: NOT RUN (no receipt)"]
    try:
        main = gitops.main_worktree(cfg.root)
        receipt = read_receipt(path, main)
        reasons: list[str] = []
        script = (cfg.assent_dir / "verify.py").resolve()
        if receipt.verify_script_sha256 != _sha256(script):
            reasons.append("verifier changed")
        target_branch = gitops.require_current_branch(main)
        if gitops.commit_of(main, target_branch) != receipt.target_tip:
            reasons.append("target tip changed")
        _branch, source_tip, _worktree = _source_snapshot(cfg, main)
        if source_tip != receipt.source_tip:
            reasons.append("source tip changed")
        if receipt.status != "PASSED":
            reasons.append(f"exit code {receipt.exit_code}")
    except AssentError as e:
        return [f"Folder verification: INVALID ({e})"]

    freshness = "fresh" if not reasons else "stale: " + "; ".join(reasons)
    lines = [
        f"Folder verification: {receipt.status} ({freshness})",
        f"  Source tip: {receipt.source_tip}",
        f"  Candidate tree: {receipt.integration_tree}",
        f"  Completed at: {receipt.completed_at}",
    ]
    if receipt.failure_summary:
        lines.append("  Failure: " + receipt.failure_summary.splitlines()[0])
    return lines


# --- Batch receipt: one full verification covering several folders ------------
#
# One candidate tree merges every queued folder in a recorded order, is verified
# once, and is then released folder by folder.  Each intermediate merge commit
# must be comparable against the receipt, so the receipt stores the tree after
# every step, not only the final tree.

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


@dataclass(frozen=True)
class BatchCandidate:
    """The rebuilt merge chain: either every step tree, or the first conflict."""

    step_trees: tuple[str, ...] = ()
    conflict_folder: str = ""
    conflicts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflict_folder


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
    _invalidate_receipt(path)
    return existed


def _batch_receipt_text(receipt: BatchVerificationReceipt) -> str:
    text = (
        f"version = {receipt.version}\n"
        f"status = {_toml_string(receipt.status)}\n"
        f"target_tip = {_toml_string(receipt.target_tip)}\n"
        f"final_tree = {_toml_string(receipt.final_tree)}\n"
        f"verify_script_sha256 = {_toml_string(receipt.verify_script_sha256)}\n"
        f"verify_command = {_toml_string(receipt.verify_command)}\n"
        f"exit_code = {receipt.exit_code}\n"
        f"completed_at = {_toml_string(receipt.completed_at)}\n"
        f"failure_summary = {_toml_string(receipt.failure_summary)}\n"
    )
    for source in receipt.sources:
        text += (
            "\n[[sources]]\n"
            f"folder = {_toml_string(source.folder)}\n"
            f"source_tip = {_toml_string(source.source_tip)}\n"
            f"step_tree = {_toml_string(source.step_tree)}\n"
        )
    return text


def _require_oid(value: object, name: str) -> None:
    if not isinstance(value, str) or not _OID_RE.fullmatch(value):
        raise AssentError(
            f"Batch verification receipt {name} must be a 40- or 64-character "
            "lowercase hexadecimal object id")


def _validate_batch_receipt(receipt: BatchVerificationReceipt,
                            repository: Path) -> None:
    """Fail closed on any incomplete, inconsistent, or non-existent identity."""
    if type(receipt.version) is not int or receipt.version != BATCH_RECEIPT_VERSION:
        raise AssentError(
            f"Batch verification receipt version must be {BATCH_RECEIPT_VERSION}")
    if receipt.status not in _RECEIPT_STATUSES:
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
        _validate_tasks_name(source.folder, "Batch verification receipt folder")
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
    if not isinstance(receipt.verify_script_sha256, str) or not _DIGEST_RE.fullmatch(
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
    if len(receipt.failure_summary) > _SUMMARY_LIMIT:
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
    _atomic_write_text(path, _batch_receipt_text(receipt))


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


def _merge_chain(candidate: Path,
                 sources: Sequence[tuple[str, str]]) -> BatchCandidate:
    """Merge every ``(folder, source_tip)`` into an open candidate worktree.

    Each step is asserted before the next one starts: a no-fast-forward merge
    must produce exactly the two expected parents, the previous step and this
    folder's source.  Anything else (a source already contained in the chain, so
    that Git reports "already up to date" and creates no commit) is an
    unexpected shape rather than a conflict, and fails closed instead of
    recording a step tree that no release could reproduce.
    """
    step_trees: list[str] = []
    for folder, source_tip in sources:
        previous = gitops.commit_of(candidate, "HEAD")
        message = f"verify(batch/{folder}): temporary integration candidate"
        outcome = gitops.merge_no_ff(candidate, source_tip, message)
        if not outcome.ok:
            return BatchCandidate(
                tuple(step_trees), folder, tuple(outcome.conflicts))
        if gitops.commit_parents(candidate, "HEAD") != (previous, source_tip):
            raise AssentError(
                f"merging {folder} did not produce the expected two-parent "
                "batch candidate")
        step_trees.append(gitops.tree_of(candidate, "HEAD"))
    return BatchCandidate(tuple(step_trees))


def build_batch_candidate(main: Path, target_tip: str,
                          sources: Sequence[tuple[str, str]]) -> BatchCandidate:
    """Merge every ``(folder, source_tip)`` in order and return each step tree.

    The chain is built in one temporary worktree that is always removed, and the
    first conflicting folder stops it.  Every step is a no-fast-forward merge, so
    the trees recorded here are exactly the trees a release reproduces.
    """
    if not sources:
        raise AssentError("a batch candidate needs at least one source folder")
    with gitops.temporary_integration_worktree(
            main, "batch", target_tip) as (candidate, _branch):
        return _merge_chain(candidate, sources)


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


# --- assent verify --batch: one full verification for every queued folder ------


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
        failure_summary=_summary(failure_summary),
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
        _branch, source_tip, _worktree = _source_snapshot(cfg, main)
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
    current, tip, worktree = _source_snapshot(cfg, main)
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
            _branch, current, _worktree = _source_snapshot(configs[folder], main)
        except AssentError as e:
            changed.append(f"source for {folder} changed: {e}")
        else:
            if current != source_tip:
                changed.append(f"source tip for {folder} changed")
    if _sha256(script) != digest:
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


def _verify_prefix(main: Path, target_tip: str,
                   sources: Sequence[tuple[str, str]],
                   script: Path) -> _PrefixRun:
    """Build and fully verify one prefix of an already-mergeable batch chain.

    The whole chain merged cleanly before localization started, so truncating it
    repeats a subset of the same merges and cannot conflict.  A conflict here
    would mean the repository changed underneath the search, which fails closed
    rather than being recorded as a test failure.
    """
    with gitops.temporary_integration_worktree(
            main, "batch", target_tip) as (candidate, _branch):
        chain = _merge_chain(candidate, sources)
        if not chain.ok:
            raise AssentError(
                f"merging {chain.conflict_folder} conflicts while localizing a "
                "batch failure, although the full chain merged cleanly; the "
                "repository changed during verification")
        try:
            result = _run_full_verifier(script, candidate)
        except OSError as e:
            return _PrefixRun(False, chain.step_trees, 1,
                              f"Unable to start verification: {e}")
    if result.returncode == 0:
        return _PrefixRun(True, chain.step_trees, 0, "")
    return _PrefixRun(
        False, chain.step_trees, result.returncode,
        _summary(result.stdout, result.stderr,
                 f"Verification command failed: {VERIFY_COMMAND} "
                 f"(exit code {result.returncode})"))


def bisect_batch_failure(main: Path, target_tip: str,
                         sources: Sequence[tuple[str, str]], script: Path,
                         failure_summary: str) -> BatchBisectResult:
    """Localize a failed batch to the first folder whose merge turns it red.

    The caller has already proven that the full chain merges cleanly and that
    verifying all of it fails, so the search looks for the smallest failing
    prefix by bisection: at most ``ceil(log2(N))`` full verifications instead of
    the ``N`` a folder-by-folder walk would cost.  Batching exists to spend fewer
    full runs, so localizing it must be cheap too.
    """
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
        run = _verify_prefix(main, target_tip, prefix, script)
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
    _tree, outcome = _candidate_tree(
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
    return _summary(note, result.guilty_summary)


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
        for folder, source_tip in selection.sources:
            if exact:
                _branch, current, _worktree = _explicit_source_snapshot(
                    configs[folder], main)
            else:
                _branch, current, _worktree = _source_snapshot(
                    configs[folder], main)
            if current != source_tip:
                raise AssentError(
                    f"source tip for {folder} changed while the batch locks "
                    "were being acquired")
        script = (assent_dir / "verify.py").resolve()
        if not script.is_file():
            raise AssentError(f"Verification script not found: {script}")
        digest = _sha256(script)

        print("verify --batch: merging "
              f"{len(selection.sources)} folder(s) in dependency order: "
              + ", ".join(selection.folders))
        _invalidate_receipt(path)

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
                chain = _merge_chain(candidate, selection.sources)
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
                try:
                    result = _run_full_verifier(script, candidate)
                except OSError as e:
                    start_failure = f"Unable to start verification: {e}"
            else:
                chain = _merge_chain_skipping_conflicts(
                    candidate, selection.sources, graph)
                if chain.conflicts:
                    _report_batch_conflicts(chain, main, target_tip)
                if chain.conflicts and not chain.sources:
                    # Nothing is left to offer, so there is no decision to ask for.
                    refusal = ("every queued folder conflicts, so no independent "
                               "subset remains to verify")
                elif chain.conflicts and not confirm(_skip_question(chain)):
                    refusal = "the skip was declined, so nothing was verified"
                else:
                    try:
                        result = _run_full_verifier(script, candidate)
                    except OSError as e:
                        start_failure = f"Unable to start verification: {e}"
                if refusal:
                    print(f"verify --batch: refused, {refusal}. The target and "
                          "every source were left unchanged and no receipt was "
                          "written")
                    return 1
                batch_sources = chain.sources
                batch_step_trees = chain.step_trees
                batch_folders = chain.folders
                batch_skipped = chain.skipped

        # A human skip or a localization may shrink what the dynamic receipt
        # certifies. Exact selection starts from the complete requested set and
        # only bisection may retain a smaller verified prefix.
        sources = batch_sources
        step_trees = batch_step_trees
        if start_failure:
            status, exit_code, summary = "FAILED", 1, start_failure
        else:
            assert result is not None
            status = "PASSED" if result.returncode == 0 else "FAILED"
            exit_code = result.returncode
            summary = "" if result.returncode == 0 else _summary(
                result.stdout, result.stderr,
                f"Verification command failed: {VERIFY_COMMAND} "
                f"(exit code {result.returncode})")
            if status == "FAILED" and bisect:
                bisected = bisect_batch_failure(
                    main, target_tip, batch_sources, script, summary)
                summary = _report_localization(
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
            failure_summary=summary)

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
