"""Reproducible, unattended full verification of one integration candidate.

The receipt written here is a derived runtime cache.  It records facts about an
explicit source snapshot, the resulting integration tree, and the main-tree
verification script; it is not human approval and never advances a Git ref.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tomllib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from assent import AssentError, gitops
from assent.config import Config
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


def write_receipt(path: Path, receipt: VerificationReceipt,
                  repository: Path) -> None:
    """Atomically replace a receipt after validating its schema and Git objects."""
    path = Path(path)
    _validate_receipt(receipt, repository)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(_receipt_text(receipt))
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
    # erase it.  Validate it before candidate construction or invalidation;
    # an explicit refresh still replaces every valid receipt below.
    if path.exists():
        read_receipt(path, main)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        raise AssentError(f"Unable to invalidate old verification receipt {path}: {e}") from e

    plan = Plan.parse(cfg.tasks_dir)
    unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                  if task.status not in _COMPLETE_STATUSES]
    if unfinished:
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
                failure_summary=f"Integration conflict: {conflicts}")
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
