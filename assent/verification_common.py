"""Low-level pieces shared by every verification module.

Only what more than one verification module genuinely needs lives here: the
receipt-field vocabulary both receipt schemas validate against, the atomic
write and digest helpers both of them use, the source-identity snapshot the
folder and batch paths both take, the two candidate builders (one folder
merged into the target, and an ordered chain of folders) that both the batch
freshness rules and the batch execution path rebuild, and the provisioned
root-level directory links the folder and batch runs both mirror into a
candidate before starting the full verifier.

This module deliberately imports none of ``folder_verification``,
``batch_receipt``, or ``batch_verification``, so those three stay independent
leaves above it.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
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
class ProvisionedLink:
    """One root-level directory link a source worktree provisions explicitly.

    ``name`` is the immediate-child name inside the worktree and ``target`` is
    the already-resolved directory it points at, so two worktrees offering the
    same name can be compared by target without touching the filesystem again.
    """

    name: str
    target: Path


def _is_directory_link(path: Path) -> bool:
    """True for a POSIX symlink, a Windows directory symlink, or a junction.

    ``os.path.islink`` is False for a Windows junction, so the reparse tag is
    checked as well.  ``st_reparse_tag`` exists only on Windows, and
    ``IO_REPARSE_TAG_MOUNT_POINT`` only there too, hence the guarded lookups
    rather than a platform test.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return (mount_point is not None
            and getattr(info, "st_reparse_tag", None) == mount_point)


def discover_worktree_links(worktree: Path) -> tuple[ProvisionedLink, ...]:
    """List the immediate-child directory links a source worktree provisions.

    Only the root level is scanned, and only links: an ordinary ignored
    directory, an ignored file, a link to a file, a dangling link, and anything
    nested inside a linked target are all left alone.  A directory link whose
    target cannot be resolved is a refusal rather than a silent omission,
    because verification would otherwise run against a candidate the human
    believes was provisioned.
    """
    worktree = Path(worktree)
    try:
        with os.scandir(worktree) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError as e:
        raise AssentError(
            f"Unable to inspect source worktree {worktree} for provisioned "
            f"directory links: {e}") from e
    links: list[ProvisionedLink] = []
    for name in names:
        path = worktree / name
        if name == ".git" or not _is_directory_link(path):
            continue
        if not path.is_dir():           # a file link or a dangling link
            continue
        try:
            target = Path(os.path.realpath(path, strict=True))
        except OSError as e:
            raise AssentError(
                f"source worktree link {worktree / name} cannot be resolved to "
                f"a directory target: {e}") from e
        if not target.is_dir():
            raise AssentError(
                f"source worktree link {worktree / name} resolves to {target}, "
                "which is not a directory")
        links.append(ProvisionedLink(name, target))
    return tuple(links)


def union_worktree_links(
        worktrees: Iterable[Path | None]) -> tuple[ProvisionedLink, ...]:
    """Union the provisioned links of every source worktree entering a candidate.

    The same relative name resolving to the same target in two worktrees is one
    link, not a conflict.  The same name resolving to different targets has no
    correct answer, so it fails closed before anything is created.
    """
    merged: dict[str, ProvisionedLink] = {}
    for worktree in worktrees:
        if worktree is None:
            continue
        for link in discover_worktree_links(worktree):
            existing = merged.get(link.name)
            if existing is None:
                merged[link.name] = link
            elif existing.target != link.target:
                raise AssentError(
                    "source worktrees provision conflicting targets for the "
                    f"root-level link {link.name}: {existing.target} and "
                    f"{link.target}")
    return tuple(merged[name] for name in sorted(merged))


def _create_directory_link(destination: Path, target: Path) -> None:
    """Create one directory link, preferring a junction on Windows.

    A Windows directory symlink needs a privilege an unattended run cannot
    assume, while a junction needs none, so Windows always gets a junction
    regardless of which kind the source worktree used.
    """
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(destination))
    else:
        os.symlink(target, destination, target_is_directory=True)


def _remove_directory_link(destination: Path) -> None:
    """Remove one mirrored link only; the target it points at is never touched.

    Both a Windows junction and a Windows directory symlink are removed with
    ``rmdir`` on the reparse point itself, and a POSIX symlink with ``unlink``.
    Neither call descends into the target.
    """
    if os.name == "nt":
        os.rmdir(destination)
    else:
        os.unlink(destination)


@contextlib.contextmanager
def provisioned_candidate_links(
        candidate: Path,
        links: Sequence[ProvisionedLink]) -> Iterator[tuple[ProvisionedLink, ...]]:
    """Mirror provisioned source links into a candidate for the verifier run only.

    The links exist only while the full verifier runs: they are created after
    the candidate's merge commits are already made and removed before the
    temporary worktree is, so the committed candidate tree never changes and
    ``git worktree remove`` never sees a reparse point to walk into.  Removal
    unlinks the mirror alone, so an interrupted or failed run leaves both the
    external target and the persistent source-worktree link untouched.

    A destination that already exists in the candidate is a refusal: a
    provisioned link may add an ignored path, never replace candidate content.
    A destination Git does not ignore there is skipped rather than mirrored.
    """
    created: list[Path] = []
    mirrored: list[ProvisionedLink] = []
    primary_error: BaseException | None = None
    try:
        for link in links:
            destination = candidate / link.name
            if os.path.lexists(destination):
                raise AssentError(
                    f"the integration candidate already contains {link.name}; a "
                    "provisioned source link must never replace candidate "
                    "content")
            if not gitops.is_path_ignored(candidate, link.name, directory=True):
                continue
            try:
                _create_directory_link(destination, link.target)
            except OSError as e:
                raise AssentError(
                    f"unable to mirror the provisioned source link {link.name} "
                    f"-> {link.target} into the integration candidate: "
                    f"{e}") from e
            created.append(destination)
            mirrored.append(link)
        if mirrored:
            print("Provisioned candidate link(s): "
                  + ", ".join(f"{link.name} -> {link.target}"
                              for link in mirrored), flush=True)
        yield tuple(mirrored)
    except BaseException as e:
        primary_error = e
        raise
    finally:
        problems: list[str] = []
        for destination in reversed(created):
            try:
                _remove_directory_link(destination)
            except OSError as e:
                problems.append(f"unable to remove mirrored link {destination}: {e}")
        if problems:
            cleanup_error = AssentError(
                "Provisioned candidate link cleanup was incomplete: "
                + "; ".join(problems))
            if primary_error is None:
                raise cleanup_error
            primary_error.add_note(str(cleanup_error))


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
