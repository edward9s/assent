"""Low-level pieces shared by every verification module.

Only what more than one verification module genuinely needs lives here: the
receipt-field vocabulary both receipt schemas validate against, the atomic
write and digest helpers both of them use, the source-identity snapshot the
folder and batch paths both take, the two candidate builders (one folder
merged into the target, and an ordered chain of folders) that both the batch
freshness rules and the batch execution path rebuild, the provisioned
directory links and ignored leaf files the folder and batch runs both mirror
into a candidate before starting the full verifier, and the ignored-input
diagnosis both of them append when a failing verifier names a path inside an
ignored source directory that was deliberately not mirrored.

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
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from assent import AssentError, gitops, pathops
from assent.config import Config

VERIFY_COMMAND = "python .assent/verify.py"
RECEIPT_STATUSES = ("PASSED", "FAILED")
OID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
SUMMARY_LIMIT = 4000
FULL_VERIFY_OUTCOMES = (
    "PASSED", "VERIFIER_FAILED", "TARGET_CONFLICT", "PEER_CONFLICT",
    "INFRASTRUCTURE_FAILED",
)


@dataclass(frozen=True)
class FullVerifyEvidence:
    """Typed, source-bound result of one complete verification transaction."""

    outcome: str
    folders: tuple[str, ...]
    target_commit: str
    source_commits: tuple[str, ...]
    candidate_tree: str
    verification_script_sha256: str
    shared_inputs_sha256: str
    exit_code: int
    evidence: tuple[str, ...] = ()
    reused: bool = False

    @property
    def passed(self) -> bool:
        return self.outcome == "PASSED"


class CandidateConflict(AssentError):
    """Candidate construction conflict carrying receipt-independent evidence."""

    def __init__(self, result: FullVerifyEvidence):
        super().__init__(result.evidence[0] if result.evidence else result.outcome)
        self.result = result


def _utf8_environment() -> dict[str, str]:
    """Give Python verifier processes a stable UTF-8 stdio contract."""
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _decode_verifier_output(output: bytes | str | None,
                            stream: str) -> tuple[str, bool]:
    """Decode one verifier pipe while preserving evidence of bad bytes."""
    if output is None:
        return "", False
    if isinstance(output, str):
        return output, False
    try:
        return output.decode("utf-8"), False
    except UnicodeDecodeError:
        return output.decode("utf-8", errors="backslashreplace"), True


def _encoding_diagnostics(streams: Iterable[str]) -> str:
    return "\n".join(
        f"Verifier output on {stream} was not valid UTF-8; "
        "undecodable bytes are escaped as \\xNN."
        for stream in streams
    )


def _append_encoding_diagnostics(stderr: str,
                                 streams: Iterable[str]) -> str:
    diagnostics = _encoding_diagnostics(streams)
    if not diagnostics:
        return stderr
    if stderr and not stderr.endswith(("\n", "\r")):
        stderr += "\n"
    return f"{stderr}{diagnostics}\n"


def _decoded_completed_process(
        result: subprocess.CompletedProcess[bytes | str]
        ) -> subprocess.CompletedProcess[str]:
    """Return text output without letting a bad verifier pipe escape."""
    stdout, bad_stdout = _decode_verifier_output(result.stdout, "stdout")
    stderr, bad_stderr = _decode_verifier_output(result.stderr, "stderr")
    bad_streams = tuple(
        stream for stream, bad in (("stdout", bad_stdout), ("stderr", bad_stderr))
        if bad
    )
    stderr = _append_encoding_diagnostics(stderr, bad_streams)
    returncode = result.returncode
    if bad_streams and returncode == 0:
        returncode = 1
    if (not bad_streams and isinstance(result.stdout, str)
            and isinstance(result.stderr, str)):
        return result
    return subprocess.CompletedProcess(
        result.args, returncode, stdout, stderr)


def invalidate_receipt(path: Path) -> None:
    """Remove stale derived evidence before starting a replacement run."""
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        raise AssentError(
            f"Unable to invalidate old verification receipt {path}: {e}") from e


def sha256_file(path: Path, label: str = "verification script") -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as e:
        raise AssentError(f"Unable to read {label} {path}: {e}") from e
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
            capture_output=True, env=_utf8_environment())
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
    result = _decoded_completed_process(result)
    elapsed = time.monotonic() - started
    print("Full verification finished: "
          f"elapsed {elapsed:.1f}s, exit code {result.returncode}",
          flush=True)
    return result


DIRECTORY_LINK = "directory"
FILE_LINK = "file"
_EXCLUDED_ROOTS = (".git", ".assent")


@dataclass(frozen=True)
class ProvisionedLink:
    """One artifact a source worktree keeps outside Git and a candidate needs.

    ``path`` is the normalized project-relative path inside the worktree, which
    may be nested below tracked parents, and ``kind`` says what is mirrored
    there: a ``directory`` link, or an ordinary ignored leaf ``file`` sitting
    beside tracked sources.  ``target`` is the already-resolved source-side path
    and ``digest`` the file's content digest (empty for a directory), so two
    worktrees offering the same relative path can be compared without touching
    the filesystem again.
    """

    path: str
    target: Path
    kind: str = DIRECTORY_LINK
    digest: str = ""

    @property
    def is_directory(self) -> bool:
        return self.kind == DIRECTORY_LINK


def _require_safe_relative(relative: str) -> None:
    """Refuse anything that is not a plain relative path inside the worktree."""
    parts = relative.split("/")
    if (not relative or relative.startswith("/") or ":" in parts[0]
            or any(part in ("", ".", "..") for part in parts)):
        raise AssentError(
            f"source worktree entry {relative!r} is not a safe relative path "
            "and cannot be provisioned into an integration candidate")


def _excluded(relative: str) -> bool:
    return relative.split("/")[0] in _EXCLUDED_ROOTS


def _has_linked_parent(worktree: Path, relative: str) -> bool:
    """True when any parent directory of ``relative`` is itself a link.

    A file below an undiscovered link is a descendant of someone else's linked
    tree, not a source-adjacent leaf, so it is left alone instead of dragging
    the traversal into that target.
    """
    parts = relative.split("/")[:-1]
    current = worktree
    for part in parts:
        current = current / part
        if pathops.is_link(current):
            return True
    return False


def _classify_link(worktree: Path, relative: str) -> ProvisionedLink:
    """Resolve one source-worktree link into a directory or file artifact."""
    path = worktree / relative
    try:
        target = Path(os.path.realpath(path, strict=True))
    except OSError as e:
        raise AssentError(
            f"source worktree link {path} cannot be resolved to an existing "
            f"target: {e}") from e
    if target.is_dir():
        return ProvisionedLink(relative, target, DIRECTORY_LINK)
    if target.is_file():
        return ProvisionedLink(relative, target, FILE_LINK,
                               sha256_file(target, "provisioned source file"))
    raise AssentError(
        f"source worktree link {path} resolves to {target}, which is neither "
        "a directory nor a file")


def discover_worktree_links(worktree: Path) -> tuple[ProvisionedLink, ...]:
    """List the artifacts a source worktree holds outside Git for the verifier.

    Two kinds qualify, at the root or nested below tracked parents: an
    explicitly provisioned directory link, and an ordinary ignored leaf file
    that sits inside an otherwise tracked directory tree, such as a generated
    ``*.g.dart`` beside its tracked source.  Git's own ignore walk supplies the
    enumeration with whole ignored trees collapsed, so an ignored directory,
    build output, a cache, and everything nested inside any linked target are
    pruned rather than listed as independent inputs.  A link whose target
    cannot be resolved is a refusal rather than a silent omission, because
    verification would otherwise run against a candidate the human believes was
    provisioned.
    """
    worktree = Path(worktree)
    directories: list[ProvisionedLink] = []
    files: list[ProvisionedLink] = []
    for entry in gitops.ignored_entries(worktree):
        relative = entry.rstrip("/")
        if _excluded(relative):
            continue
        _require_safe_relative(relative)
        path = worktree / relative
        if pathops.is_link(path):
            link = _classify_link(worktree, relative)
            (directories if link.is_directory else files).append(link)
        elif entry.endswith("/"):
            continue                    # an ordinary ignored directory tree
        elif _has_linked_parent(worktree, relative):
            continue                    # a descendant of someone's linked tree
        else:
            files.append(ProvisionedLink(
                relative, path, FILE_LINK,
                sha256_file(path, "provisioned source file")))
    inside = tuple(f"{link.path}/" for link in directories)
    leaves = [link for link in files if not link.path.startswith(inside)]
    return tuple(sorted(directories + leaves, key=lambda link: link.path))


IGNORED_INPUT_PREFIX = "Ignored input diagnosis: "


def ordinary_ignored_directories(worktree: Path) -> tuple[str, ...]:
    """List the physical ignored directory trees a source worktree holds.

    These are exactly the trees ``discover_worktree_links`` deliberately does
    not mirror: a real directory Git ignores, not a provisioned link.  Git's own
    collapsed ignore walk supplies the names, and only the entry itself is
    stat-ed, so no ignored tree is traversed to answer the question.
    """
    worktree = Path(worktree)
    found: list[str] = []
    for entry in gitops.ignored_entries(worktree):
        if not entry.endswith("/"):
            continue
        relative = entry.rstrip("/")
        if _excluded(relative) or pathops.is_link(worktree / relative):
            continue
        try:
            _require_safe_relative(relative)
        except AssentError:
            continue
        found.append(relative)
    return tuple(sorted(found))


def _mentions_path_below(text: str, relative: str) -> bool:
    """True when ``text`` names something inside the ``relative`` directory.

    The whole directory name must match at a path boundary, so ``mypkg/x`` does
    not answer for ``pkg``, and a mention of the bare directory with nothing
    below it is not a failing input either.
    """
    pattern = (r"(?:^|[^0-9A-Za-z_.\-])" + re.escape(relative)
               + r"/[0-9A-Za-z_.\-]")
    return re.search(pattern, text) is not None


def mentioned_ordinary_ignored_directories(
        output: str, worktrees: Iterable[Path | None]) -> tuple[str, ...]:
    """Ignored directories whose descendant path the output actually names."""
    text = output.replace("\\", "/")
    named: set[str] = set()
    for worktree in worktrees:
        if worktree is None:
            continue
        for relative in ordinary_ignored_directories(worktree):
            if _mentions_path_below(text, relative):
                named.add(relative)
    return tuple(sorted(named))


def ignored_input_diagnosis(output: str,
                            worktrees: Iterable[Path | None]) -> str:
    """Explain a verifier failure that names a physically ignored source path.

    A candidate is built from tracked content plus the two mirrored artifact
    kinds, so an ordinary ignored directory a source worktree happens to hold --
    a copied package tree, for instance -- is simply not there.  The verifier
    then fails on a path nobody provisioned, which reads like a broken change
    rather than a missing input.

    Only the directories whose own path the captured output actually names are
    reported, after Windows separators are normalized, so unrelated ignored
    trees are neither listed nor traversed.  An empty string means the failure
    says nothing about an ignored input.
    """
    named = mentioned_ordinary_ignored_directories(output, worktrees)
    if not named:
        return ""
    listed = ", ".join(f"{relative}/" for relative in sorted(named))
    subject = ("is an ordinary ignored directory" if len(named) == 1
               else "are ordinary ignored directories")
    return (
        f"{IGNORED_INPUT_PREFIX}{listed} {subject} in a contributing source "
        "worktree, so it is intentionally omitted from the integration "
        "candidate; complete verification mirrors only provisioned directory "
        "links and ignored leaf files, never a physical ignored tree.\n"
        "For a required input, place its ordinary Git-ignored target at the "
        "same relative path in the primary worktree, then record it with "
        "`assent shared-paths review` -- naming the dependency or build file "
        "that made it necessary as a `--watch` value. Assent provisions the "
        "exact junction or directory symlink for later sessions. Do not copy "
        "the tree or hand-create a source-worktree link; neither is reviewed "
        "candidate evidence.")


def diagnosed_ignored_directories(failure_summary: str) -> tuple[str, ...]:
    """The directories a stored ``Ignored input diagnosis:`` names, if any.

    This reads back exactly what ``ignored_input_diagnosis`` wrote, so a full
    verifier's own output-backed evidence can invalidate a reviewed shared-path
    profile that does not declare a directory the run proved necessary.  It
    parses one recorded line and never touches the filesystem.
    """
    for line in failure_summary.splitlines():
        if not line.startswith(IGNORED_INPUT_PREFIX):
            continue
        listed = line[len(IGNORED_INPUT_PREFIX):]
        for tail in (" is an ordinary", " are ordinary"):
            head, separator, _rest = listed.partition(tail)
            if separator:
                return tuple(
                    entry.strip().rstrip("/") for entry in head.split(",")
                    if entry.strip().rstrip("/"))
    return ()


def print_ignored_input_diagnosis(label: str, failure_summary: str) -> None:
    """Surface a stored ignored-input diagnosis on a truncated failure line.

    ``summary`` keeps the tail of an oversized capture and the diagnosis is
    appended last, so it is still there to print even when the verifier output
    ahead of it was truncated.
    """
    lines = failure_summary.splitlines()
    for index, line in enumerate(lines):
        if line.startswith(IGNORED_INPUT_PREFIX):
            for rest in lines[index:]:
                print(f"{label}: {rest}")
            return


def _require_no_overlap(links: Sequence[ProvisionedLink]) -> None:
    """Refuse a set where one provisioned path lives inside another one."""
    for index, link in enumerate(links):
        for other in links[index + 1:]:
            if other.path.startswith(f"{link.path}/"):
                raise AssentError(
                    f"source worktrees provision overlapping paths: {other.path} "
                    f"lies inside {link.path}")


def union_worktree_links(
        worktrees: Iterable[Path | None]) -> tuple[ProvisionedLink, ...]:
    """Union the provisioned artifacts of every source worktree in a candidate.

    The same relative path resolving to the same directory target, or to a file
    with the same content digest, is one artifact rather than a conflict.
    Differing targets, differing file contents, a path that is a directory in
    one worktree and a file in another, and a path nested inside another
    worktree's link have no correct answer, so they fail closed before anything
    is created.
    """
    merged: dict[str, ProvisionedLink] = {}
    for worktree in worktrees:
        if worktree is None:
            continue
        for link in discover_worktree_links(worktree):
            existing = merged.get(link.path)
            if existing is None:
                merged[link.path] = link
            elif existing.kind != link.kind:
                raise AssentError(
                    f"source worktrees provision {link.path} as both a "
                    f"{existing.kind} and a {link.kind}")
            elif existing.is_directory and existing.target != link.target:
                raise AssentError(
                    "source worktrees provision conflicting targets for the "
                    f"directory link {link.path}: {existing.target} and "
                    f"{link.target}")
            elif not existing.is_directory and existing.digest != link.digest:
                raise AssentError(
                    "source worktrees provision differing contents for the "
                    f"ignored file {link.path}: {existing.target} and "
                    f"{link.target}")
    ordered = tuple(merged[path] for path in sorted(merged))
    _require_no_overlap(ordered)
    return ordered


def _create_file_link(destination: Path, target: Path) -> None:
    """Link one source file into the candidate without copying its content.

    Windows gets a hard link, which needs no privilege but does need both paths
    on one volume; POSIX gets a file symlink.  A platform that cannot create
    the link raises ``OSError`` and the caller fails closed, because a copy
    would silently detach the candidate from the file the source is using.
    """
    if os.name == "nt":
        os.link(target, destination)
    else:
        os.symlink(target, destination)


def _remove_link(destination: Path, kind: str) -> None:
    """Remove one mirrored link only; the target it points at is never touched.

    A directory link goes through the same non-traversing detachment Git
    worktree removal uses; a mirrored file is a POSIX symlink or a Windows hard
    link, which ``unlink`` removes.  No call descends into the target.
    """
    if kind == DIRECTORY_LINK:
        pathops.detach_directory_link(destination)
    else:
        os.unlink(destination)


def _create_parents(candidate: Path, relative: str) -> list[Path]:
    """Create the missing parent directories of one mirrored path.

    Only genuinely missing directories are created, and each one is returned so
    cleanup removes exactly those and nothing that the candidate tree or an
    earlier artifact already owned.
    """
    created: list[Path] = []
    current = candidate
    for part in relative.split("/")[:-1]:
        current = current / part
        if current.is_dir() and not pathops.is_link(current):
            continue
        if os.path.lexists(current):
            raise AssentError(
                f"unable to provision {relative} into the integration "
                f"candidate: {current} exists and is not a directory")
        try:
            current.mkdir()
        except OSError as e:
            raise AssentError(
                f"unable to create the parent directory {current} for the "
                f"provisioned artifact {relative}: {e}") from e
        created.append(current)
    return created


@contextlib.contextmanager
def provisioned_candidate_links(
        candidate: Path,
        links: Sequence[ProvisionedLink]) -> Iterator[tuple[ProvisionedLink, ...]]:
    """Mirror provisioned source artifacts into a candidate for the verifier run.

    The links exist only while the full verifier runs: they are created after
    the candidate's merge commits are already made and removed before the
    temporary worktree is, so the committed candidate tree never changes and
    ``git worktree remove`` never sees a reparse point to walk into.  Removal
    unlinks the mirrors alone, deepest path first, and then removes only the
    empty parent directories this function created, so an interrupted or failed
    run leaves both the external target and the persistent source-worktree link
    untouched.

    A destination that already exists in the candidate is a refusal: a
    provisioned artifact may add an ignored path, never replace or shadow
    candidate content.  A destination Git does not ignore there is skipped
    rather than mirrored.
    """
    created: list[tuple[Path, str]] = []
    parents: list[Path] = []
    mirrored: list[ProvisionedLink] = []
    primary_error: BaseException | None = None
    try:
        for link in sorted(links, key=lambda link: link.path):
            destination = candidate / link.path
            if os.path.lexists(destination):
                raise AssentError(
                    f"the integration candidate already contains {link.path}; a "
                    "provisioned source link must never replace candidate "
                    "content")
            if not gitops.is_path_ignored(candidate, link.path,
                                          directory=link.is_directory):
                continue
            parents.extend(_create_parents(candidate, link.path))
            try:
                if link.is_directory:
                    pathops.create_directory_link(destination, link.target)
                else:
                    _create_file_link(destination, link.target)
            except OSError as e:
                raise AssentError(
                    f"unable to mirror the provisioned source {link.kind} "
                    f"{link.path} -> {link.target} into the integration "
                    f"candidate: {e}") from e
            created.append((destination, link.kind))
            mirrored.append(link)
        if mirrored:
            print("Provisioned candidate link(s): "
                  + ", ".join(f"{link.path} -> {link.target}"
                              for link in mirrored), flush=True)
        yield tuple(mirrored)
    except BaseException as e:
        primary_error = e
        raise
    finally:
        problems: list[str] = []
        for destination, kind in sorted(
                created, key=lambda item: len(item[0].parts), reverse=True):
            try:
                _remove_link(destination, kind)
            except OSError as e:
                problems.append(f"unable to remove mirrored link {destination}: {e}")
        for parent in sorted(parents, key=lambda path: len(path.parts),
                             reverse=True):
            try:
                parent.rmdir()
            except OSError as e:
                problems.append(
                    f"unable to remove the provisioned parent directory "
                    f"{parent}: {e}")
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
