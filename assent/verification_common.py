"""Low-level pieces shared by every verification module.

Only what more than one verification module genuinely needs lives here: the
receipt-field vocabulary both receipt schemas validate against, the atomic
write and digest helpers both of them use, the source-identity snapshot the
folder and batch paths both take, and the two candidate builders (one folder
merged into the target, and an ordered chain of folders) that both the batch
freshness rules and the batch execution path rebuild.

This module deliberately imports none of ``folder_verification``,
``batch_receipt``, or ``batch_verification``, so those three stay independent
leaves above it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from assent import AssentError, gitops
from assent.config import Config

VERIFY_COMMAND = "python .assent/verify.py"
RECEIPT_STATUSES = ("PASSED", "FAILED")
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SUMMARY_LIMIT = 4000


def invalidate_receipt(path: Path) -> None:
    """Remove stale derived evidence before starting a replacement run."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        raise AssentError(
            f"Unable to invalidate old verification receipt {path}: {e}") from e


def sha256_file(path: Path) -> str:
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
    return sha256_file(script)


def summary(*parts: str) -> str:
    """Normalize child diagnostics and bound receipt growth."""
    text = "\n".join(part.strip() for part in parts if part and part.strip())
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(
        character if (character in "\n\t" or ord(character) >= 0x20)
        and character != "\ufffd" else "?"
        for character in text
    )
    if len(text) > SUMMARY_LIMIT:
        marker = "...[earlier output truncated]\n"
        text = marker + text[-(SUMMARY_LIMIT - len(marker)):]
    return text


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def require_oid(value: object, name: str, label: str) -> None:
    """Refuse anything that is not a 40- or 64-character lowercase object id."""
    if not isinstance(value, str) or not OID_RE.fullmatch(value):
        raise AssentError(
            f"{label} {name} must be a 40- or 64-character "
            "lowercase hexadecimal object id")


def atomic_write_text(path: Path, text: str) -> None:
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


def source_snapshot(cfg: Config, main: Path) -> tuple[str, str, Path | None]:
    """Resolve one folder's single clean source branch, tip, and worktree."""
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


def candidate_tree(main: Path, folder: str, target_tip: str,
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


def run_full_verifier(script: Path,
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


@dataclass(frozen=True)
class BatchCandidate:
    """The rebuilt merge chain: either every step tree, or the first conflict."""

    step_trees: tuple[str, ...] = ()
    conflict_folder: str = ""
    conflicts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.conflict_folder


def merge_chain(candidate: Path,
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

    Both the batch freshness rules and the batch execution path rebuild the same
    chain, so the primitive lives here rather than in either of them.
    """
    if not sources:
        raise AssentError("a batch candidate needs at least one source folder")
    with gitops.temporary_integration_worktree(
            main, "batch", target_tip) as (candidate, _branch):
        return merge_chain(candidate, sources)
