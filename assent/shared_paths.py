"""Reviewed shared ignored directories: a project-local cache, not a source contract.

Some projects genuinely need an ignored directory to exist inside a working
tree before their own tests can run -- a vendored package tree, a generated
asset directory, a localization ``arb`` folder.  Git cannot carry it and no
filesystem rule can prove which ignored directory is semantically required, so
the answer has to be reviewed once by a human or an AI session and then reused.

This module owns that answer and everything around it:

* ``.assent/manifest.toml`` in the primary worktree -- one untracked,
  Assent-owned file of local execution memory.  It is not project source, not
  receipt evidence and not an acceptance input, and it is never committed.
  Named top-level tables keep it extensible; this module owns ``[shared_paths]``
  alone and a top-level ``version`` governs compatible future sections.
* Reviewed *profiles*, retained by fingerprint rather than overwritten, so two
  branches with different dependency structure each keep their own prior answer
  instead of making the cache oscillate.
* The three-state contract a scheduled session starts under -- UNKNOWN,
  REVIEWED-NONE, REVIEWED-PATHS -- plus STALE, which is a matched answer that
  concrete evidence has invalidated, and NO-IGNORED-DIRECTORY-CANDIDATE, the
  deterministic fast path for a primary worktree a successful Git query proves
  holds no ordinary ignored directory to declare at all.
* The controlled review operation, the only writer of the manifest, and the
  provisioning that turns a reviewed profile into real directory links using
  ``pathops.create_directory_link`` -- the same primitive candidate mirroring
  uses.

Nothing here ever traverses, copies, modifies or deletes a link target.  A link
this module created is detached as a link object; anything else found in a
declared place is a refusal, never a replacement.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import tomllib
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from assent import AssentError, gitops, pathops

MANIFEST_NAME = "manifest.toml"
MANIFEST_LOCK_NAME = "manifest.lock"
SCHEMA_VERSION = 1
SECTION = "shared_paths"

UNKNOWN = "UNKNOWN"
REVIEWED_NONE = "REVIEWED-NONE"
REVIEWED_PATHS = "REVIEWED-PATHS"
STALE = "STALE"
# A successful Git ignored-entry query of the primary worktree that found no
# existing ordinary ignored directory outside `.git/` and `.assent/`.  It is a
# statement about that query alone and never a claim that the project needs no
# shared input semantically: nothing exists there that anyone could declare, so
# it neither charges a session for a review nor refuses a verification.  It is
# still its own identity in the shared-input digest, distinct from the reviewed
# empty answer REVIEWED-NONE.  A failed query is not this state -- it is a
# refusal -- and the moment a candidate directory appears, classification
# becomes UNKNOWN.
NO_IGNORED_DIRECTORY_CANDIDATE = "NO-IGNORED-DIRECTORY-CANDIDATE"

ABSENT = "absent"                       # a watched path that is not there at all
_EXCLUDED_ROOTS = (".git", ".assent")
_IGNORE_RULE_PATHSPEC = "*.gitignore"
REVIEW_COMMAND = "assent shared-paths review"


# --------------------------------------------------------------------------- #
# Manifest model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Profile:
    """One reviewed answer, keyed by the source snapshot it was reviewed for.

    ``paths`` are normalized project-relative directories (empty means the
    reviewed answer "none are required"), ``watch`` the exact tracked
    dependency/build files that justify reconsidering it, and ``digests`` the
    per-file evidence -- each watch file plus every tracked Git-ignore rule file
    -- from which ``fingerprint`` is derived.  Keeping the digests, not only
    their hash, is what lets a stale profile report *which* file changed instead
    of merely that something did.
    """

    fingerprint: str
    paths: tuple[str, ...] = ()
    watch: tuple[str, ...] = ()
    digests: dict[str, str] = field(default_factory=dict)

    @property
    def is_none(self) -> bool:
        return not self.paths


@dataclass(frozen=True)
class Application:
    """What assent actually linked into one managed source worktree."""

    worktree: str
    fingerprint: str
    paths: tuple[str, ...] = ()


@dataclass
class Manifest:
    """The parsed local manifest; unknown top-level tables are preserved."""

    version: int = SCHEMA_VERSION
    profiles: tuple[Profile, ...] = ()
    applications: tuple[Application, ...] = ()
    other: dict = field(default_factory=dict)
    present: bool = False

    def matching(self, digests: dict[str, str]) -> tuple[Profile, ...]:
        """Every stored profile whose recorded evidence still matches the tree."""
        return tuple(profile for profile in self.profiles
                     if _matches(profile, digests))


@dataclass(frozen=True)
class Contract:
    """The shared-path state one source worktree starts a task session under.

    ``needs_review`` separates "no answer and something to decide" from "no
    answer and nothing to decide": a primary worktree that a successful Git
    query proves holds no ordinary ignored directory has nothing anyone could
    declare, so no session is charged for discovering that.
    """

    state: str
    profile: Profile | None = None
    digests: dict[str, str] = field(default_factory=dict)
    prior_paths: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    needs_review: bool = False

    @property
    def paths(self) -> tuple[str, ...]:
        return self.profile.paths if self.profile is not None else ()

    @property
    def settled(self) -> bool:
        """True when nothing is left to decide before real work may start."""
        return self.state in (REVIEWED_NONE, REVIEWED_PATHS,
                              NO_IGNORED_DIRECTORY_CANDIDATE)


# --------------------------------------------------------------------------- #
# Paths, locking and atomic replacement
# --------------------------------------------------------------------------- #
def manifest_path(main: Path) -> Path:
    """The one manifest, always in the primary worktree's ``.assent``."""
    return Path(main) / ".assent" / MANIFEST_NAME


def manifest_lock_path(main: Path) -> Path:
    return Path(main) / ".assent" / MANIFEST_LOCK_NAME


if os.name == "nt":                     # pragma: no cover - platform specific
    import msvcrt

    def _try_lock(handle) -> bool:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:                                   # pragma: no cover - platform specific
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextlib.contextmanager
def hold_manifest_lock(main: Path) -> Iterator[None]:
    """Serialize manifest writers on one project-local lock file.

    A second review attempt that cannot take the lock is refused rather than
    queued: the caller learns that another review is in flight and no update is
    silently lost.  The lock file itself lives beside the manifest in the
    primary worktree's ``.assent``, so it shares the manifest's untracked,
    Assent-owned status.
    """
    path = manifest_lock_path(main)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise AssentError(
            f"Unable to prepare the shared-path manifest directory "
            f"{path.parent}: {e}") from e
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    handle = os.fdopen(os.open(str(path), flags, 0o644), "r+b")
    try:
        if not _try_lock(handle):
            raise AssentError(
                "Another shared-path review is already updating "
                f"{manifest_path(main)}; only one may write at a time")
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid = {os.getpid()}\n".encode("utf-8"))
            handle.flush()
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


def _atomic_write(path: Path, text: str) -> None:
    """Replace the manifest in place: a reader sees the old file or the new one."""
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as e:
        raise AssentError(
            f"Unable to atomically write the shared-path manifest {path}: {e}") from e
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Reading and writing the manifest
# --------------------------------------------------------------------------- #
def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise AssentError(f"{label} must be a list of strings")
    return tuple(value)


def _profile_from(data: object, path: Path) -> Profile:
    if not isinstance(data, dict):
        raise AssentError(f"{path}: each [{SECTION}] profile must be a table")
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise AssentError(f"{path}: a shared-path profile needs a fingerprint")
    paths = _string_list(data.get("paths", []), f"{path}: profile paths")
    watch = _string_list(data.get("watch", []), f"{path}: profile watch")
    digests = data.get("digests", {})
    if not isinstance(digests, dict) or any(
            not isinstance(v, str) for v in digests.values()):
        raise AssentError(f"{path}: profile digests must be a table of strings")
    for relative in (*paths, *watch):
        require_safe_relative(relative, path)
    return Profile(fingerprint, tuple(paths), tuple(watch), dict(digests))


def _application_from(data: object, path: Path) -> Application:
    if not isinstance(data, dict):
        raise AssentError(f"{path}: each [{SECTION}] application must be a table")
    worktree = data.get("worktree")
    fingerprint = data.get("fingerprint")
    if not isinstance(worktree, str) or not worktree:
        raise AssentError(f"{path}: an application record needs a worktree")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise AssentError(f"{path}: an application record needs a fingerprint")
    paths = _string_list(data.get("paths", []), f"{path}: application paths")
    for relative in paths:
        require_safe_relative(relative, path)
    return Application(worktree, fingerprint, tuple(paths))


def read_manifest(main: Path) -> Manifest:
    """Parse the local manifest; a missing file is simply an empty one.

    Malformed TOML, an unsupported schema version, an unsafe declared path and a
    wrongly shaped table are refusals rather than a silent empty answer: the
    cache decides whether real links are created, so an unreadable cache must
    never be mistaken for "nothing was reviewed".
    """
    path = manifest_path(main)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return Manifest()
    except OSError as e:
        raise AssentError(
            f"Unable to read the shared-path manifest {path}: {e}") from e
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise AssentError(
            f"The shared-path manifest {path} is not valid TOML: {e}") from e

    version = data.get("version", SCHEMA_VERSION)
    if not isinstance(version, int) or version < 1:
        raise AssentError(f"{path}: version must be a positive integer")
    if version > SCHEMA_VERSION:
        raise AssentError(
            f"{path}: schema version {version} was written by a newer assent "
            f"(this one understands {SCHEMA_VERSION})")

    section = data.get(SECTION, {})
    if not isinstance(section, dict):
        raise AssentError(f"{path}: [{SECTION}] must be a table")
    profiles = tuple(_profile_from(entry, path)
                     for entry in section.get("profile", []))
    applications = tuple(_application_from(entry, path)
                         for entry in section.get("application", []))
    other = {key: value for key, value in data.items()
             if key not in (SECTION, "version")}
    return Manifest(version, profiles, applications, other, present=True)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise AssentError(
        f"the shared-path manifest cannot represent {type(value).__name__} values")


def _render_table(name: str, table: dict, array: bool = False) -> list[str]:
    header = f"[[{name}]]" if array else f"[{name}]"
    lines = [header]
    nested: list[str] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.extend(_render_table(f"{name}.{key}", value))
        elif isinstance(value, list) and value and all(
                isinstance(item, dict) for item in value):
            for item in value:
                nested.extend(_render_table(f"{name}.{key}", item, array=True))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    return lines + ([""] + nested if nested else [])


def _toml_key(key: str) -> str:
    plain = key and all(
        character.isalnum() or character in "-_" for character in key)
    return key if plain else json.dumps(key, ensure_ascii=False)


def render_manifest(manifest: Manifest) -> str:
    """Serialize the manifest, keeping unknown top-level tables verbatim."""
    lines = [
        "# assent local shared-path manifest -- Assent-owned execution memory.",
        "# Untracked and never committed: it records reviewed decisions for this",
        "# machine, not project source, receipt evidence or acceptance input.",
        f"version = {manifest.version}",
        "",
    ]
    for name, value in manifest.other.items():
        if isinstance(value, dict):
            lines.extend(_render_table(name, value))
            lines.append("")
        else:
            lines.insert(4, f"{_toml_key(name)} = {_toml_value(value)}")
    for profile in manifest.profiles:
        lines.extend(_render_table(f"{SECTION}.profile", {
            "fingerprint": profile.fingerprint,
            "paths": list(profile.paths),
            "watch": list(profile.watch),
            "digests": dict(sorted(profile.digests.items())),
        }, array=True))
        lines.append("")
    for application in manifest.applications:
        lines.extend(_render_table(f"{SECTION}.application", {
            "worktree": application.worktree,
            "fingerprint": application.fingerprint,
            "paths": list(application.paths),
        }, array=True))
        lines.append("")
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text


def write_manifest(main: Path, manifest: Manifest) -> None:
    """Replace the manifest atomically after proving the result parses."""
    text = render_manifest(manifest)
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:  # pragma: no cover - defensive
        raise AssentError(
            f"refusing to write an unparseable shared-path manifest: {e}") from e
    _atomic_write(manifest_path(main), text)


# --------------------------------------------------------------------------- #
# Fingerprints
# --------------------------------------------------------------------------- #
def require_safe_relative(relative: str, owner: object = "") -> str:
    """Normalize and refuse anything that is not a plain in-repository path."""
    if not isinstance(relative, str) or not relative.strip():
        raise AssentError(
            f"{owner}: a shared path must be a non-empty project-relative path"
            if owner else
            "a shared path must be a non-empty project-relative path")
    normalized = relative.replace("\\", "/").strip().rstrip("/")
    parts = normalized.split("/")
    label = f"{owner}: " if owner else ""
    if (not normalized or normalized.startswith("/") or ":" in parts[0]
            or any(part in ("", ".", "..") for part in parts)):
        raise AssentError(
            f"{label}{relative!r} is not a safe project-relative path")
    if parts[0] in _EXCLUDED_ROOTS:
        raise AssentError(
            f"{label}{relative!r} is inside {parts[0]}, which assent owns and "
            "never shares")
    return normalized


def _digest_of(path: Path) -> str:
    """The content digest of one file, or ``ABSENT`` when it is not there."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError,
            PermissionError):
        return ABSENT
    except OSError as e:
        raise AssentError(f"Unable to read the watched file {path}: {e}") from e
    return digest.hexdigest()


def ignore_rule_files(worktree: Path) -> tuple[str, ...]:
    """Every tracked Git-ignore rule file in the source snapshot."""
    try:
        tracked = gitops.tracked_paths(Path(worktree), _IGNORE_RULE_PATHSPEC)
    except AssentError:
        return ()
    return tuple(sorted(
        entry for entry in tracked
        if entry.rsplit("/", 1)[-1] == ".gitignore"))


def snapshot_digests(worktree: Path,
                     watch: Sequence[str]) -> dict[str, str]:
    """The evidence one profile is fingerprinted from, read from the worktree.

    Two kinds of file decide whether a reviewed answer still holds: the exact
    dependency/build files the review declared, and the repository's own tracked
    Git-ignore rules, since those decide which directories are ignored at all.
    A file that is not there is recorded as ``absent`` rather than omitted, so a
    watched file disappearing is as visible as one changing.
    """
    worktree = Path(worktree)
    digests: dict[str, str] = {}
    for relative in sorted(set(watch) | set(ignore_rule_files(worktree))):
        digests[relative] = _digest_of(worktree / relative)
    return digests


def fingerprint_of(digests: dict[str, str]) -> str:
    """Hash one evidence snapshot into the profile key used for lookup."""
    digest = hashlib.sha256()
    digest.update(b"assent-shared-paths-v1\n")
    for relative in sorted(digests):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(digests[relative].encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _matches(profile: Profile, current: dict[str, str]) -> bool:
    """True when every file the profile recorded still has its recorded digest.

    The comparison is over the profile's own recorded keys, so two profiles with
    different watch sets are each answered against their own evidence.
    """
    if not profile.digests:
        return False
    return all(current.get(relative, ABSENT) == recorded
               for relative, recorded in profile.digests.items())


def changed_watch_evidence(profile: Profile,
                           current: dict[str, str]) -> tuple[str, ...]:
    """Name only the watched files whose state actually differs, and how."""
    changes: list[str] = []
    for relative, recorded in sorted(profile.digests.items()):
        now = current.get(relative, ABSENT)
        if now == recorded:
            continue
        if recorded == ABSENT:
            changes.append(f"{relative} (appeared)")
        elif now == ABSENT:
            changes.append(f"{relative} (disappeared)")
        else:
            changes.append(f"{relative} (changed)")
    return tuple(changes)


# --------------------------------------------------------------------------- #
# Target validation
# --------------------------------------------------------------------------- #
def _is_ordinary_directory(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if pathops.is_link_stat(info) or pathops.is_reparse_point(info):
        return False
    return stat.S_ISDIR(info.st_mode)


def target_problem(main: Path, relative: str) -> str:
    """Why the primary worktree cannot serve ``relative`` as a shared target.

    An empty string means it can: the path is an ordinary directory there (not a
    link, not a file, not a reparse point assent cannot classify) and Git ignores
    it, so linking to it neither shadows tracked content nor exports anything
    Git is supposed to carry itself.
    """
    main = Path(main)
    path = main / relative
    if not os.path.lexists(path):
        return f"{relative} does not exist in the primary worktree {main}"
    if not _is_ordinary_directory(path):
        return (f"{relative} is not an ordinary directory in the primary "
                f"worktree {main}")
    try:
        if not gitops.is_path_ignored(main, relative, directory=True):
            return (f"{relative} is no longer Git-ignored in the primary "
                    f"worktree {main}")
    except AssentError as e:
        return f"{relative} cannot be checked against Git's ignore rules: {e}"
    return ""


# --------------------------------------------------------------------------- #
# Shared-input evidence bound into a receipt
# --------------------------------------------------------------------------- #
def _entry_kind(path: Path, info: os.stat_result) -> str:
    if pathops.is_link_stat(info):
        return "link"
    if pathops.is_reparse_point(info):
        raise AssentError(
            f"refusing to snapshot the shared target content at {path}: it is a "
            "reparse point assent cannot classify, so it is left unread rather "
            "than walked into")
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    if stat.S_ISREG(info.st_mode):
        return "file"
    raise AssentError(
        f"refusing to snapshot the shared target content at {path}: it is "
        "neither an ordinary file, an ordinary directory, nor a representable "
        f"link (mode {stat.S_IFMT(info.st_mode):#o})")


def _link_identity(path: Path) -> str:
    """A link's own target text, or a refusal when it cannot be represented.

    Only the link object is read.  A Windows junction is not a symlink to
    Python, so ``os.readlink`` is tried and an unreadable link is refused rather
    than resolved -- resolving it is exactly the traversal this must not do.
    """
    try:
        return os.readlink(path).replace("\\", "/")
    except OSError as e:
        raise AssentError(
            f"refusing to snapshot the shared target content at {path}: its "
            f"link target cannot be represented without following it ({e})"
        ) from e


def snapshot_target(main: Path, relative: str) -> str:
    """Digest one declared shared directory through a bounded, safe traversal.

    Ordinary directories are descended, ordinary files are hashed by content,
    and a link is recorded by its own target text without ever being followed --
    so nothing outside the declared target is read, and a nested link that
    escapes it changes the digest instead of widening the walk.  Any shape that
    cannot be represented unambiguously -- an unclassifiable reparse point, an
    unreadable link, a device or socket -- refuses rather than being skipped.
    """
    root = Path(main) / relative
    digest = hashlib.sha256()
    digest.update(b"assent-shared-target-v1\n")
    digest.update(relative.encode("utf-8"))
    digest.update(b"\n")
    pending = [("", root)]
    while pending:
        prefix, current = pending.pop()
        try:
            with os.scandir(current) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError as e:
            raise AssentError(
                f"Unable to read the shared target directory {current}: {e}") from e
        for name in names:
            path = current / name
            key = f"{prefix}{name}"
            if "/" in name or name in ("", ".", ".."):  # pragma: no cover
                raise AssentError(
                    f"refusing to snapshot the shared target content at {path}: "
                    f"{name!r} is not a representable entry name")
            try:
                info = os.lstat(path)
            except OSError as e:
                raise AssentError(
                    f"Unable to inspect {path} while snapshotting the shared "
                    f"target {relative}: {e}") from e
            kind = _entry_kind(path, info)
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(kind.encode("utf-8"))
            digest.update(b"\0")
            if kind == "link":
                digest.update(_link_identity(path).encode("utf-8"))
            elif kind == "file":
                content = _digest_of(path)
                if content == ABSENT:       # pragma: no cover - raced removal
                    raise AssentError(
                        f"Unable to read {path} while snapshotting the shared "
                        f"target {relative}")
                digest.update(content.encode("utf-8"))
            else:
                pending.append((f"{key}/", path))
            digest.update(b"\n")
    return digest.hexdigest()


def shared_inputs_digest(main: Path,
                         contracts: Sequence[tuple[str, Contract]]) -> str:
    """One deterministic digest of every shared input a verification depended on.

    It covers, in the caller's own contributing order, each source's folder name
    and selected profile fingerprint, that profile's normalized declared paths,
    the exact resolved primary-worktree target of each one, and a content
    snapshot of that target.  REVIEWED-NONE contributes an explicit empty-profile
    line, so "reviewed to need nothing" is evidence and is never confused with
    UNKNOWN, which has no digest at all because it may not reach a receipt.
    """
    digest = hashlib.sha256()
    digest.update(b"assent-shared-inputs-v1\n")
    snapshots: dict[str, str] = {}
    for folder, contract in contracts:
        if not contract.settled:
            raise AssentError(
                f"refusing to record shared-input evidence for {folder}: its "
                f"shared-path contract is {contract.state}, not a reviewed answer")
        fingerprint = contract.profile.fingerprint if contract.profile else ""
        digest.update(f"{folder}\0{contract.state}\0{fingerprint}\n"
                      .encode("utf-8"))
        for relative in contract.paths:
            if relative not in snapshots:
                snapshots[relative] = snapshot_target(main, relative)
            digest.update(
                f"{relative}\0{_link_target(main, relative).as_posix()}\0"
                f"{snapshots[relative]}\n".encode("utf-8"))
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def has_ignored_directory_candidate(worktree: Path) -> bool:
    """True when the worktree really holds an ordinary ignored directory.

    This is the whole content of the NO-IGNORED-DIRECTORY-CANDIDATE fast path:
    nothing exists there that anyone could declare as a shared input, so an
    absent manifest is not an unanswered question and no session is asked to
    discover that.  ``.git`` and ``.assent`` never count, an ignored leaf file
    is not a candidate, and a directory that is not an ordinary directory --
    a junction, a symlink, a path that has since gone -- is not one either.
    Any remaining ignored directory does count, even one a review would go on
    to answer with ``paths = []``.

    Failure is never an answer: an unusable Git query raises rather than
    reporting False, because being unable to inspect the ignored entries is not
    evidence that there are none.

    The question is always asked of the *primary* worktree: that is where a
    shared input really lives and where every allowed link target must be, and
    a fresh source checkout legitimately has none of them yet -- which is
    precisely what a review exists to fix.
    """
    root = Path(worktree)
    for entry in gitops.ignored_entries(root):
        if not entry.endswith("/"):
            continue                    # an ignored leaf file is not a candidate
        relative = entry.rstrip("/")
        if relative.split("/")[0] in _EXCLUDED_ROOTS:
            continue
        # Only the entry itself is examined; a link is never followed and no
        # ignored tree is ever walked.
        if not _is_ordinary_directory(root / relative):
            continue
        return True
    return False


def _evidence_note(relative: str) -> str:
    return (f"{relative} is required by complete-verifier evidence but no "
            "reviewed profile declares it")


def _required_evidence_paths(main: Path,
                             required_evidence: Iterable[str]) -> tuple[str, ...]:
    """Normalize verifier-required directories, refusing unusable ones.

    A complete verifier that names a required ignored directory has produced a
    subject for review -- but only the primary worktree can serve one, so a
    named directory that is missing there, is not an ordinary directory, or is
    no longer ignored cannot be reviewed into existence.  That is reported as
    the exact target problem rather than as a review clause the session could
    never satisfy, and never as "nothing needs sharing".
    """
    normalized: list[str] = []
    problems: list[str] = []
    for entry in required_evidence:
        relative = require_safe_relative(entry)
        if relative in normalized:
            continue
        problem = target_problem(main, relative)
        if problem:
            problems.append(problem)
        normalized.append(relative)
    if problems:
        raise AssentError(
            "complete-verifier evidence requires a shared ignored directory "
            f"that cannot be provisioned: {'; '.join(problems)}. Create the "
            f"directory in the primary worktree and keep it Git-ignored, then "
            f"run `{REVIEW_COMMAND}`")
    return tuple(sorted(normalized))


def classify(main: Path, worktree: Path,
             manifest: Manifest | None = None,
             required_evidence: Iterable[str] = ()) -> Contract:
    """Decide the shared-path state one source worktree starts under.

    UNKNOWN means no stored profile answers this snapshot at all; REVIEWED-NONE
    and REVIEWED-PATHS are a matching profile's answer, the empty one included --
    an empty reviewed answer is an answer and must never trigger another review
    merely for being empty.  STALE is a previously reviewed project whose answer
    no longer holds: the watched evidence moved, a declared target changed, or
    ``required_evidence`` (a complete verifier naming a required ignored
    directory) contradicts the active profile.  Two matching profiles that
    disagree have no correct answer and fail closed.

    NO-IGNORED-DIRECTORY-CANDIDATE is reached only when nothing was ever
    reviewed, no verifier evidence demands anything, and a *successful* Git
    query proves the primary worktree holds no ordinary ignored directory.  A
    failed query and a required directory that the primary worktree cannot
    serve are both refusals, never that fast path.
    """
    main = Path(main)
    worktree = Path(worktree)
    manifest = read_manifest(main) if manifest is None else manifest

    watch = sorted({entry for profile in manifest.profiles
                    for entry in profile.watch})
    digests = snapshot_digests(worktree, watch)
    matches = manifest.matching(digests)

    if len({profile.paths for profile in matches}) > 1:
        listed = "; ".join(
            f"{profile.fingerprint[:12]} -> {list(profile.paths)}"
            for profile in matches)
        raise AssentError(
            f"the shared-path manifest {manifest_path(main)} holds conflicting "
            f"matching profiles ({listed}); resolve them with "
            f"`{REVIEW_COMMAND}` before any session runs")

    if not matches:
        prior = manifest.profiles[-1].paths if manifest.profiles else ()
        evidence = tuple(
            change for profile in manifest.profiles
            for change in changed_watch_evidence(profile, digests))
        # Verifier evidence naming a required ignored directory is a real
        # subject on its own: it must never settle as "there is nothing to
        # declare", and a directory the primary worktree cannot serve is
        # refused with that exact problem instead of being queued for a review
        # that could not succeed.
        required = _required_evidence_paths(main, required_evidence)
        if manifest.profiles:
            state = STALE
        elif required or has_ignored_directory_candidate(main):
            state = UNKNOWN
        else:
            state = NO_IGNORED_DIRECTORY_CANDIDATE
        evidence += tuple(_evidence_note(relative) for relative in required)
        return Contract(state, None, digests, prior, tuple(dict.fromkeys(evidence)),
                        needs_review=state in (STALE, UNKNOWN))

    profile = matches[0]
    problems = tuple(
        problem for problem in
        (target_problem(main, relative) for relative in profile.paths)
        if problem)
    required = set(_required_evidence_paths(main, required_evidence))
    missing = tuple(sorted(required - set(profile.paths)))
    if problems or missing:
        evidence = problems + tuple(_evidence_note(relative)
                                    for relative in missing)
        return Contract(STALE, profile, digests, profile.paths, evidence,
                        needs_review=True)
    return Contract(REVIEWED_NONE if profile.is_none else REVIEWED_PATHS,
                    profile, digests, profile.paths)


# --------------------------------------------------------------------------- #
# Provisioning and reconciliation
# --------------------------------------------------------------------------- #
def _link_target(main: Path, relative: str) -> Path:
    return (Path(main) / relative).resolve()


def _resolves_to(path: Path, target: Path) -> bool:
    """True when ``path`` is a link resolving exactly to ``target``.

    Only the link itself is examined and only its target's identity is asked
    for; nothing inside either one is enumerated.
    """
    if not pathops.is_link(path):
        return False
    try:
        return Path(os.path.realpath(path, strict=True)) == target
    except OSError:
        return False


def _provision_one(main: Path, worktree: Path, relative: str) -> bool:
    """Ensure one exact link exists; return whether this call created it."""
    target = _link_target(main, relative)
    destination = Path(worktree) / relative
    if os.path.lexists(destination):
        if _resolves_to(destination, target):
            return False
        raise AssentError(
            f"refusing to provision the shared path {relative} into {worktree}: "
            f"{destination} already exists and is not a link to {target}")
    if gitops.tracked_paths(Path(worktree), relative):
        raise AssentError(
            f"refusing to provision the shared path {relative} into {worktree}: "
            "tracked content lives there and a shared link must never shadow it")
    if not gitops.is_path_ignored(Path(worktree), relative, directory=True):
        raise AssentError(
            f"refusing to provision the shared path {relative} into {worktree}: "
            "Git does not ignore it there, so the link would change what the "
            "worktree tracks")
    parent = destination.parent
    if not parent.is_dir():
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise AssentError(
                f"Unable to create the parent directory {parent} for the shared "
                f"path {relative}: {e}") from e
    try:
        pathops.create_directory_link(destination, target)
    except OSError as e:
        raise AssentError(
            f"Unable to link the shared path {relative} in {worktree} to "
            f"{target}: {e}") from e
    return True


def _detach_one(main: Path, worktree: Path, relative: str) -> bool:
    """Detach one link assent created; anything else is left exactly as found.

    The proof required is deliberately narrow: the place must still hold a link
    object that resolves to the primary worktree's same relative directory.  An
    ordinary directory, a foreign link and a missing path are all no-ops, and
    the target itself is never walked, modified or removed.
    """
    destination = Path(worktree) / relative
    if not _resolves_to(destination, _link_target(main, relative)):
        return False
    try:
        pathops.detach_directory_link(destination)
    except OSError as e:
        raise AssentError(
            f"Unable to detach the shared-path link {destination}: {e}") from e
    return True


def _record_application(manifest: Manifest, worktree: Path,
                        fingerprint: str, paths: Sequence[str]) -> bool:
    """Replace this worktree's application record; True when anything changed.

    A recorded worktree that no longer exists cannot hold a link to reconcile,
    so its stale record is discarded on sight -- by a single existence check,
    never by traversing anything.  Reporting whether the records actually moved
    is what keeps a repository with nothing to apply from being handed a
    manifest file it never needed.
    """
    key = _worktree_key(worktree)
    kept = tuple(
        application for application in manifest.applications
        if application.worktree != key
        and Path(application.worktree).is_dir())
    record = Application(key, fingerprint, tuple(paths))
    updated = kept + ((record,) if paths else ())
    if updated == manifest.applications:
        return False
    manifest.applications = updated
    return True


def _worktree_key(worktree: Path) -> str:
    return Path(worktree).resolve().as_posix()


def applied_paths(manifest: Manifest, worktree: Path) -> tuple[str, ...]:
    key = _worktree_key(worktree)
    for application in manifest.applications:
        if application.worktree == key:
            return application.paths
    return ()


def reconcile(main: Path, worktree: Path, contract: Contract, *,
              manifest: Manifest | None = None) -> tuple[tuple[str, ...],
                                                         tuple[str, ...]]:
    """Bring one source worktree in line with its reviewed profile.

    Returns ``(created, detached)``.  Additions create only genuinely missing
    exact links; a path a newer profile no longer declares is detached only when
    the recorded application says assent created it and the place still proves to
    be a link to the primary worktree's same relative directory.  Running inside
    the primary worktree itself provisions nothing -- a path cannot be a link to
    itself -- but the caller's profile caching still happens.
    """
    main = Path(main)
    worktree = Path(worktree)
    if not contract.settled:
        raise AssentError(
            f"refusing to provision shared paths for {worktree}: the source "
            f"contract is {contract.state}, not a reviewed answer")
    if _worktree_key(worktree) == _worktree_key(main):
        return (), ()

    manifest = read_manifest(main) if manifest is None else manifest
    wanted = contract.paths
    previous = applied_paths(manifest, worktree)

    for relative in wanted:
        problem = target_problem(main, relative)
        if problem:
            raise AssentError(
                f"refusing to provision shared paths for {worktree}: {problem}")

    created = tuple(relative for relative in wanted
                    if _provision_one(main, worktree, relative))
    detached = tuple(relative for relative in previous
                     if relative not in wanted
                     and _detach_one(main, worktree, relative))
    if _record_application(
            manifest, worktree,
            contract.profile.fingerprint if contract.profile else "", wanted):
        write_manifest(main, manifest)
    return created, detached


def application_problem(main: Path, worktree: Path, *,
                        manifest: Manifest | None = None) -> str:
    """Why a recorded application no longer holds, or ``""`` when it still does.

    A resumed managed worktree -- a reconciliation being continued, above all --
    is revalidated rather than silently repaired: the profile it was provisioned
    from must still be in the manifest and every recorded path must still be a
    link to the primary worktree's same relative directory.  A worktree assent
    never provisioned has no record and so no problem.
    """
    manifest = read_manifest(main) if manifest is None else manifest
    key = _worktree_key(worktree)
    record = next((application for application in manifest.applications
                   if application.worktree == key), None)
    if record is None:
        return ""
    if not any(profile.fingerprint == record.fingerprint
               for profile in manifest.profiles):
        return (f"the shared-path profile {record.fingerprint[:12]} recorded for "
                f"{worktree} is no longer in {manifest_path(main)}")
    for relative in record.paths:
        target = _link_target(main, relative)
        if not _resolves_to(Path(worktree) / relative, target):
            return (f"the shared path {relative} in {worktree} is no longer a "
                    f"link to {target}")
    return ""


def release(main: Path, worktree: Path) -> tuple[str, ...]:
    """Detach every link assent recorded for one worktree and drop the record.

    This is what a managed worktree's disposal calls before Git or any recursive
    remover is allowed near it.  Only a link object proven to point at the
    primary worktree's same relative directory is detached; an ordinary
    directory, a foreign link and an already-removed path are left exactly as
    found, and no target is ever traversed.
    """
    main = Path(main)
    key = _worktree_key(worktree)
    with hold_manifest_lock(main):
        manifest = read_manifest(main)
        record = next((application for application in manifest.applications
                       if application.worktree == key), None)
        if record is None:
            return ()
        detached = tuple(relative for relative in record.paths
                         if _detach_one(main, worktree, relative))
        manifest.applications = tuple(
            application for application in manifest.applications
            if application.worktree != key)
        write_manifest(main, manifest)
    return detached


def prepare_sources(main: Path,
                    sources: Sequence[tuple[str, Path | None]]
                    ) -> tuple[tuple[str, Contract], ...]:
    """Classify and reconcile every source a verification is about to depend on.

    This is the one gate every verification entry point goes through -- single
    folder, exact selected batch, dynamic batch, localization prefix, chained
    ``run --verify``, and ``--focus``.  Each contributing live source worktree is
    classified against the local manifest and its Assent-owned declared links are
    reconciled, so a missing one is recreated rather than silently depended on
    from a previous ``run``.  UNKNOWN, STALE, an ordinary destination, a foreign
    link, and an invalid profile all refuse here, before any verifier command
    exists, and the refusal names the zero-AI remedy.

    The returned contracts, in the caller's order, are what
    ``shared_inputs_digest`` binds into the receipt.
    """
    main = Path(main)
    prepared: list[tuple[str, Contract]] = []
    with hold_manifest_lock(main):
        manifest = read_manifest(main)
        for folder, worktree in sources:
            # A folder whose source worktree is gone has no snapshot of its own,
            # so it is classified against the primary worktree -- the same
            # receipt-backed fallback acceptance already uses.  Doing it here
            # rather than skipping keeps a later freshness check comparable.
            contract = classify(main, worktree or main, manifest)
            if not contract.settled:
                raise AssentError(
                    f"refusing to verify: the shared-path contract for "
                    f"{folder} ({worktree}) is {contract.state}. "
                    f"{closeout_refusal(contract) or 'Run `' + REVIEW_COMMAND + '`'}")
            reconcile(main, worktree or main, contract, manifest=manifest)
            prepared.append((folder, contract))
    return tuple(prepared)


def prepare_worktree(main: Path, worktree: Path, *,
                     required_evidence: Iterable[str] = ()) -> Contract:
    """Classify a source worktree and, when the answer is settled, apply it.

    This is what runs before a scheduled task session: REVIEWED-PATHS provisions
    every declared missing link, REVIEWED-NONE starts with no links and no extra
    AI instructions, and UNKNOWN or STALE touches the filesystem not at all and
    leaves the session to run the controlled review.
    """
    contract = classify(main, worktree, required_evidence=required_evidence)
    if contract.settled:
        with hold_manifest_lock(main):
            reconcile(main, worktree, contract)
    return contract


# --------------------------------------------------------------------------- #
# The controlled review operation
# --------------------------------------------------------------------------- #
def _validate_paths(main: Path, paths: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw in paths:
        relative = require_safe_relative(raw, "shared path")
        if Path(raw.replace("\\", "/")).is_absolute():
            raise AssentError(
                f"shared path {raw!r} must be project-relative, not absolute")
        problem = target_problem(main, relative)
        if problem:
            raise AssentError(f"refusing to record a shared path: {problem}")
        if relative not in normalized:
            normalized.append(relative)
    for relative in normalized:
        for other in normalized:
            if other != relative and relative.startswith(f"{other}/"):
                raise AssentError(
                    f"refusing to record overlapping shared paths: {relative} "
                    f"lies inside {other}")
    return tuple(sorted(normalized))


def _validate_watch(worktree: Path, watch: Sequence[str]) -> tuple[str, ...]:
    if not watch:
        raise AssentError(
            "a shared-path review must state at least one --watch file: without "
            "it nothing could ever make the decision worth reconsidering")
    normalized: list[str] = []
    for raw in watch:
        relative = require_safe_relative(raw, "watch file")
        if relative in normalized:
            continue
        if not gitops.tracked_paths(Path(worktree), relative):
            raise AssentError(
                f"watch file {relative} is not tracked in {worktree}; only a "
                "tracked dependency or build file can justify a review")
        path = Path(worktree) / relative
        if not path.is_file():
            raise AssentError(
                f"watch file {relative} is not a readable file in {worktree}")
        normalized.append(relative)
    return tuple(sorted(normalized))


def review(main: Path, worktree: Path, *,
           paths: Sequence[str] = (), watch: Sequence[str] = (),
           none: bool = False) -> Contract:
    """Record one reviewed shared-path profile, then reconcile this worktree.

    Every value is validated before anything is mutated: a path must be an
    existing ordinary Git-ignored directory at the same relative place in the
    primary worktree, and a watch must be a readable tracked file in the source
    snapshot.  The whole update happens under one project-local lock and lands
    through one atomic replacement, so a concurrent attempt is refused rather
    than interleaved and an interruption leaves the previous complete file.

    Recording a profile never destroys another branch's profile: an existing
    entry with the same fingerprint is replaced, everything else is retained.
    """
    main = Path(main)
    worktree = Path(worktree)
    if none and paths:
        raise AssentError(
            "a shared-path review states either --path values or --none, not both")
    if not none and not paths:
        raise AssentError(
            "a shared-path review must state at least one --path, or --none to "
            "record that this snapshot needs no shared directory")

    declared = () if none else _validate_paths(main, paths)
    watched = _validate_watch(worktree, watch)

    with hold_manifest_lock(main):
        manifest = read_manifest(main)
        digests = snapshot_digests(worktree, watched)
        profile = Profile(fingerprint_of(digests), declared, watched, digests)
        manifest.profiles = tuple(
            existing for existing in manifest.profiles
            if existing.fingerprint != profile.fingerprint) + (profile,)
        manifest.version = SCHEMA_VERSION
        write_manifest(main, manifest)

        contract = Contract(REVIEWED_NONE if profile.is_none else REVIEWED_PATHS,
                            profile, digests, profile.paths)
        reconcile(main, worktree, contract, manifest=manifest)
    return contract


# --------------------------------------------------------------------------- #
# What a session and the scheduler are told
# --------------------------------------------------------------------------- #
_REVIEW_CLAUSE = (
    "\nShared ignored directories for this repository are {state}. Before you "
    "close this task out you must run, in this worktree:\n"
    "  {command} --path DIR --watch FILE   (repeat both as needed)\n"
    "  {command} --none --watch FILE       (if no shared directory is required)\n"
    "Decide from the repository's Git-ignore rules, its dependency/build "
    "declarations and this task's verifier evidence alone -- do not audit the "
    "whole repository. Name as --watch exactly the tracked dependency or build "
    "files that would make this decision worth reconsidering.{prior}{evidence}\n"
    "The scheduler refuses this task's completion while the shared-path "
    "contract is still unreviewed.")


def review_clause(contract: Contract) -> str:
    """The bounded review instruction appended to an UNKNOWN or STALE session.

    A settled contract adds nothing at all: a reviewed answer, the empty one
    included, must not spend a session's attention on rediscovering it.
    """
    if contract.settled or not contract.needs_review:
        return ""
    prior = ("\nPreviously reviewed shared paths: "
             + ", ".join(contract.prior_paths) if contract.prior_paths else "")
    evidence = ("\nWhat changed: " + "; ".join(contract.evidence)
                if contract.evidence else "")
    return _REVIEW_CLAUSE.format(state=contract.state, command=REVIEW_COMMAND,
                                 prior=prior, evidence=evidence)


def closeout_refusal(contract: Contract) -> str:
    """The precise retry reason for a session that never settled the contract."""
    if contract.settled or not contract.needs_review:
        return ""
    evidence = f" ({'; '.join(contract.evidence)})" if contract.evidence else ""
    return (f"the shared-path contract for this source is still "
            f"{contract.state}{evidence}; run `{REVIEW_COMMAND}` with the "
            "reviewed --path/--none and --watch values before closing out")


def describe(contract: Contract) -> str:
    """One operator-facing line stating the contract a session starts under."""
    if contract.state == REVIEWED_PATHS:
        return f"Shared paths: REVIEWED-PATHS ({', '.join(contract.paths)})"
    if contract.state == REVIEWED_NONE:
        return "Shared paths: REVIEWED-NONE (no shared directory is required)"
    if contract.state == NO_IGNORED_DIRECTORY_CANDIDATE:
        return (f"Shared paths: {NO_IGNORED_DIRECTORY_CANDIDATE} (the primary "
                "worktree holds no ignored directory anyone could declare)")
    return f"Shared paths: {contract.state}; one bounded review is required"
