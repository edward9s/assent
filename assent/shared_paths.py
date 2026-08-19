"""Reviewed shared ignored directories: a project-local cache, not a source contract.

Some projects genuinely need an ignored directory to exist inside a working
tree before their own tests can run -- a vendored package tree, a generated
asset directory, a localization ``arb`` folder.  Git cannot carry it and no
filesystem rule can prove which ignored directory is semantically required, so
the answer has to be reviewed once by a human or an AI session and then reused.

This module owns that answer and everything around it:

* ``.assent/manifest.toml`` in the primary worktree -- one untracked,
  Assent-owned file of local execution memory.  It is not project source, not
  candidate content, and it is never committed; the selected profile and
  target snapshot are bound separately into verification evidence.
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
import re
import stat
import tomllib
import uuid
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from pathlib import Path

from assent import AssentError, gitops, pathops

MANIFEST_NAME = "manifest.toml"
MANIFEST_LOCK_NAME = "manifest.lock"
SCHEMA_VERSION = 2
REVIEW_CONTEXT_VERSION = 2
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
_UNTRACKED = "untracked"                # internal snapshot-only watch state
_EXCLUDED_ROOTS = (".git", ".assent")
_IGNORE_RULE_PATHSPEC = "*.gitignore"
REVIEW_COMMAND = "assent shared-paths review"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# --------------------------------------------------------------------------- #
# Manifest model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PathDisposition:
    """Why one ignored directory is deliberately not shared."""

    path: str
    reason: str


@dataclass(frozen=True)
class ValidatedReview:
    """One complete, non-mutating review decision ready to record."""

    paths: tuple[str, ...]
    watch: tuple[str, ...]
    inventory: tuple[str, ...]
    dispositions: tuple[PathDisposition, ...]


@dataclass(frozen=True)
class Profile:
    """One reviewed answer, keyed by the source snapshot it was reviewed for.

    ``paths`` are normalized project-relative directories (empty means the
    reviewed answer "none are required"), ``watch`` the exact tracked
    dependency/build files that justify reconsidering it, and ``digests`` the
    per-file evidence -- each watch file plus every tracked Git-ignore rule file
    -- from which ``fingerprint`` is derived.  Keeping the digests, not only
    their hash, is what lets a stale profile report *which* file changed instead
    of merely that something did. ``inventory`` and ``dispositions`` prove that
    every ignored directory was explicitly accounted for. The review version
    invalidates an older answer once when these semantics change.
    """

    fingerprint: str
    paths: tuple[str, ...] = ()
    watch: tuple[str, ...] = ()
    digests: dict[str, str] = field(default_factory=dict)
    review_version: int = REVIEW_CONTEXT_VERSION
    inventory: tuple[str, ...] = ()
    dispositions: tuple[PathDisposition, ...] = ()

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
    """The parsed local manifest; unknown top-level tables keep their meaning."""

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
    declare, so no session is charged for discovering that. ``inventory`` is the
    complete collapsed set of ordinary ignored directories physically present
    in the primary worktree. Presence is never proof that a directory is
    required; complete disposition coverage makes omission explicit.
    """

    state: str
    profile: Profile | None = None
    digests: dict[str, str] = field(default_factory=dict)
    prior_paths: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    needs_review: bool = False
    inventory: tuple[str, ...] = ()

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


def _stored_relative(value: object, label: str, path: Path) -> str:
    """Validate one canonical path read from Assent-owned evidence."""
    if not isinstance(value, str):
        raise AssentError(f"{path}: {label} must contain strings")
    normalized = require_safe_relative(value, path)
    if value != normalized:
        raise AssentError(
            f"{path}: {label} contains non-normalized path {value!r}; "
            f"expected {normalized!r}")
    return normalized


def _canonical_list(values: tuple[str, ...], label: str, path: Path
                    ) -> tuple[str, ...]:
    if len(set(values)) != len(values):
        raise AssentError(f"{path}: {label} contains duplicate paths")
    if values != tuple(sorted(values)):
        raise AssentError(f"{path}: {label} is not in normalized order")
    return values


def _inventory_from(value: object, path: Path, *, legacy: bool = False
                    ) -> tuple[str, ...]:
    if legacy and isinstance(value, list) and all(
            isinstance(item, dict) for item in value):
        raw_inventory_list: list[str] = []
        for index, item in enumerate(value):
            if set(item) != {"path", "kind"} or not isinstance(item["kind"], str):
                raise AssentError(
                    f"{path}: legacy profile inventory[{index}] must contain "
                    "exactly path and kind")
            raw_inventory_list.append(item["path"])
        raw_inventory = tuple(raw_inventory_list)
    else:
        raw_inventory = _string_list(value, f"{path}: profile inventory")
    return _canonical_list(tuple(
        _stored_relative(value, "profile inventory", path)
        for value in raw_inventory), "profile inventory", path)


def _dispositions_from(value: object, path: Path, *, legacy: bool = False
                       ) -> tuple[PathDisposition, ...]:
    if not isinstance(value, list):
        raise AssentError(
            f"{path}: profile dispositions must be an array of tables")
    dispositions: list[PathDisposition] = []
    for index, raw in enumerate(value):
        fields = set(raw) if isinstance(raw, dict) else set()
        valid_fields = ({"path", "reason"}, {"path", "kind", "reason"})
        if (not isinstance(raw, dict)
                or fields not in (valid_fields if legacy else valid_fields[:1])
                or ("kind" in raw and not isinstance(raw["kind"], str))):
            raise AssentError(
                f"{path}: profile dispositions[{index}] must contain exactly "
                + ("path and reason, with an optional legacy kind"
                   if legacy else "path and reason"))
        relative = _stored_relative(
            raw["path"], f"profile dispositions[{index}].path", path)
        reason = raw["reason"]
        if not isinstance(reason, str) or not reason.strip():
            raise AssentError(
                f"{path}: profile dispositions[{index}].reason must be non-empty")
        dispositions.append(PathDisposition(relative, reason.strip()))
    if len({item.path for item in dispositions}) != len(dispositions):
        raise AssentError(f"{path}: profile dispositions contain duplicate paths")
    for item in dispositions:
        if any(other.path != item.path
               and item.path.startswith(f"{other.path}/")
               for other in dispositions):
            raise AssentError(
                f"{path}: profile dispositions contain overlapping paths")
    if dispositions != sorted(dispositions, key=lambda item: item.path):
        raise AssentError(f"{path}: profile dispositions are not in normalized order")
    return tuple(dispositions)


def _validate_profile_coverage(profile: Profile, path: Path) -> None:
    """Require a current profile to account for every ignored directory."""
    if profile.review_version < REVIEW_CONTEXT_VERSION:
        return
    inventory = set(profile.inventory)
    shared = set(profile.paths)
    disposition_roots = {item.path for item in profile.dispositions}
    covered_shared = {
        relative for relative in inventory
        if any(relative == root or relative.startswith(f"{root}/")
               for root in shared)}
    covered_classified = {
        relative for relative in inventory
        if any(relative == root or relative.startswith(f"{root}/")
               for root in disposition_roots)}
    empty_shared = sorted(
        root for root in shared
        if not any(relative == root or relative.startswith(f"{root}/")
                   for relative in inventory))
    if empty_shared:
        raise AssentError(
            f"{path}: shared path(s) cover no profile inventory directory: "
            + ", ".join(empty_shared))
    empty_dispositions = sorted(
        root for root in disposition_roots
        if not any(relative == root or relative.startswith(f"{root}/")
                   for relative in inventory))
    overlap = sorted(covered_shared & covered_classified)
    if overlap:
        raise AssentError(
            f"{path}: inventory directories are both shared and excluded: "
            + ", ".join(overlap))
    missing = sorted(inventory - covered_shared - covered_classified)
    if missing or empty_dispositions:
        details = []
        if missing:
            details.append("unclassified: " + ", ".join(missing))
        if empty_dispositions:
            details.append("disposition covers no inventory directory: "
                           + ", ".join(empty_dispositions))
        raise AssentError(
            f"{path}: profile does not exactly cover ignored-directory inventory ("
            + "; ".join(details) + ")")


def _profile_from(data: object, path: Path, *, schema_version: int = SCHEMA_VERSION
                  ) -> Profile:
    if not isinstance(data, dict):
        raise AssentError(f"{path}: each [{SECTION}] profile must be a table")
    fingerprint = data.get("fingerprint")
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        raise AssentError(
            f"{path}: a shared-path profile fingerprint must be a 64-character "
            "lowercase SHA-256 digest")
    raw_paths = _string_list(data.get("paths", []), f"{path}: profile paths")
    raw_watch = _string_list(data.get("watch", []), f"{path}: profile watch")
    review_version = data.get("review_version", 1)
    if (not isinstance(review_version, int) or isinstance(review_version, bool)
            or review_version < 1):
        raise AssentError(
            f"{path}: profile review_version must be a positive integer")
    if (schema_version >= SCHEMA_VERSION
            and review_version > REVIEW_CONTEXT_VERSION):
        raise AssentError(
            f"{path}: profile review_version {review_version} was written by a "
            "newer assent")
    if schema_version < SCHEMA_VERSION:
        review_version = min(review_version, REVIEW_CONTEXT_VERSION - 1)
    if not raw_watch:
        raise AssentError(
            f"{path}: a shared-path profile needs at least one watch file")
    paths = _canonical_list(tuple(
        _stored_relative(value, "profile paths", path) for value in raw_paths),
        "profile paths", path)
    watch = _canonical_list(tuple(
        _stored_relative(value, "profile watch", path) for value in raw_watch),
        "profile watch", path)
    digests = data.get("digests", {})
    if not isinstance(digests, dict):
        raise AssentError(f"{path}: profile digests must be a table of strings")
    normalized_digests: dict[str, str] = {}
    for raw_relative, digest in digests.items():
        relative = _stored_relative(raw_relative, "profile digest keys", path)
        if not isinstance(digest, str) or (
                digest != ABSENT and not _SHA256_RE.fullmatch(digest)):
            raise AssentError(
                f"{path}: profile digest for {relative} must be a 64-character "
                f"lowercase SHA-256 digest or {ABSENT!r}")
        normalized_digests[relative] = digest
    missing_watch = [relative for relative in watch
                     if relative not in normalized_digests]
    if missing_watch:
        raise AssentError(
            f"{path}: profile watch file(s) lack recorded digest evidence: "
            + ", ".join(missing_watch))
    if fingerprint != fingerprint_of(normalized_digests):
        raise AssentError(
            f"{path}: profile fingerprint does not match its digest evidence")
    legacy = schema_version < SCHEMA_VERSION
    inventory = _inventory_from(data.get("inventory", []), path, legacy=legacy)
    dispositions = _dispositions_from(
        data.get("dispositions", []), path, legacy=legacy)
    profile = Profile(
        fingerprint, paths, watch, normalized_digests, review_version,
        inventory, dispositions)
    _validate_profile_coverage(profile, path)
    return profile


def _application_from(data: object, path: Path) -> Application:
    if not isinstance(data, dict):
        raise AssentError(f"{path}: each [{SECTION}] application must be a table")
    worktree = data.get("worktree")
    fingerprint = data.get("fingerprint")
    if not isinstance(worktree, str) or not worktree:
        raise AssentError(f"{path}: an application record needs a worktree")
    if not isinstance(fingerprint, str) or not _SHA256_RE.fullmatch(fingerprint):
        raise AssentError(
            f"{path}: an application record needs a 64-character lowercase "
            "SHA-256 fingerprint")
    raw_paths = _string_list(data.get("paths", []), f"{path}: application paths")
    paths = _canonical_list(tuple(
        _stored_relative(value, "application paths", path) for value in raw_paths),
        "application paths", path)
    return Application(worktree, fingerprint, paths)


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
    raw_profiles = section.get("profile", [])
    raw_applications = section.get("application", [])
    if not isinstance(raw_profiles, list):
        raise AssentError(f"{path}: [{SECTION}].profile must be an array of tables")
    if not isinstance(raw_applications, list):
        raise AssentError(
            f"{path}: [{SECTION}].application must be an array of tables")
    profiles = tuple(_profile_from(entry, path, schema_version=version)
                     for entry in raw_profiles)
    applications = tuple(_application_from(entry, path)
                         for entry in raw_applications)
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
    if isinstance(value, datetime):
        return value.isoformat(" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise AssentError(
        f"the shared-path manifest cannot represent {type(value).__name__} values")


def _render_table(path: tuple[str, ...], table: dict,
                  array: bool = False) -> list[str]:
    name = ".".join(_toml_key(segment) for segment in path)
    header = f"[[{name}]]" if array else f"[{name}]"
    lines = [header]
    nested: list[str] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.extend(_render_table(path + (key,), value))
        elif isinstance(value, list) and value and all(
                isinstance(item, dict) for item in value):
            for item in value:
                nested.extend(_render_table(path + (key,), item, array=True))
        else:
            lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    return lines + ([""] + nested if nested else [])


def _toml_key(key: str) -> str:
    plain = bool(re.fullmatch(r"[A-Za-z0-9_-]+", key))
    return key if plain else json.dumps(key, ensure_ascii=False)


def render_manifest(manifest: Manifest) -> str:
    """Serialize the manifest while preserving supported unknown meaning.

    TOML has no round-trip guarantee for comments, ordering, or whitespace, so
    unknown tables are reconstructed semantically.  Their standard scalar and
    nested table values are retained; an unsupported shape raises before the
    atomic replacement starts.
    """
    lines = [
        "# assent local shared-path manifest -- Assent-owned execution memory.",
        "# Untracked and never committed: it records reviewed decisions for this",
        "# machine, not project source, receipt evidence or acceptance input.",
        f"version = {manifest.version}",
        "",
    ]
    for name, value in manifest.other.items():
        if isinstance(value, dict):
            lines.extend(_render_table((name,), value))
            lines.append("")
        else:
            lines.insert(4, f"{_toml_key(name)} = {_toml_value(value)}")
    for profile in manifest.profiles:
        lines.extend(_render_table((SECTION, "profile"), {
            "fingerprint": profile.fingerprint,
            "review_version": profile.review_version,
            "paths": list(profile.paths),
            "watch": list(profile.watch),
            "digests": dict(sorted(profile.digests.items())),
            "inventory": list(profile.inventory),
            "dispositions": [
                {"path": item.path, "reason": item.reason}
                for item in profile.dispositions],
        }, array=True))
        lines.append("")
    for application in manifest.applications:
        lines.extend(_render_table((SECTION, "application"), {
            "worktree": application.worktree,
            "fingerprint": application.fingerprint,
            "paths": list(application.paths),
        }, array=True))
        lines.append("")
    text = "\n".join(lines).rstrip("\n") + "\n"
    return text


def write_manifest(main: Path, manifest: Manifest) -> None:
    """Replace the manifest atomically after proving the result parses."""
    manifest_file = manifest_path(main)
    # Programmatic callers are subject to the same evidence checks as TOML
    # readers.  This keeps a malformed Profile from being written first and
    # discovered only by the next classification gate.
    for profile in manifest.profiles:
        _profile_from({
            "fingerprint": profile.fingerprint,
            "review_version": profile.review_version,
            "paths": list(profile.paths),
            "watch": list(profile.watch),
            "digests": dict(profile.digests),
            "inventory": list(profile.inventory),
            "dispositions": [
                {"path": item.path, "reason": item.reason}
                for item in profile.dispositions],
        }, manifest_file)
    for application in manifest.applications:
        _application_from({
            "worktree": application.worktree,
            "fingerprint": application.fingerprint,
            "paths": list(application.paths),
        }, manifest_file)
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
    # A failed query is an actionable classification failure.  Turning it into
    # an empty set could make an old profile look current and spend verification
    # or acceptance evidence on a snapshot whose ignore rules were never read.
    tracked = gitops.tracked_paths(Path(worktree), _IGNORE_RULE_PATHSPEC)
    return tuple(sorted(
        entry for entry in tracked
        if entry.rsplit("/", 1)[-1] == ".gitignore"))


def snapshot_digests(worktree: Path,
                     watch: Sequence[str], *,
                     ignore_files: Sequence[str] | None = None
                     ) -> dict[str, str]:
    """The evidence one profile is fingerprinted from, read from the worktree.

    Two kinds of file decide whether a reviewed answer still holds: the exact
    dependency/build files the review declared, and the repository's own tracked
    Git-ignore rules, since those decide which directories are ignored at all.
    A file that is not there is recorded as ``absent`` rather than omitted, so a
    watched file disappearing is as visible as one changing.
    """
    worktree = Path(worktree)
    digests: dict[str, str] = {}
    if ignore_files is None:
        ignore_files = ignore_rule_files(worktree)
    for relative in sorted(set(watch) | set(ignore_files)):
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


def _evidence_matches(profile: Profile, current: dict[str, str]) -> bool:
    """True when every file the profile recorded still has its recorded digest."""
    if set(profile.digests) != set(current):
        return False
    if fingerprint_of(current) != profile.fingerprint:
        return False
    return all(current[relative] == recorded
               for relative, recorded in profile.digests.items())


def _matches(
        profile: Profile, current: dict[str, str], *,
        inventory: tuple[str, ...] | None = None) -> bool:
    """True when file evidence and review-context semantics both still match.

    The comparison is over the profile's own recorded keys, so two profiles with
    different watch sets are each answered against their own evidence.
    """
    inventory = profile.inventory if inventory is None else inventory
    return (profile.review_version == REVIEW_CONTEXT_VERSION
            and _evidence_matches(profile, current)
            and profile.inventory == inventory)


def _profile_snapshots(manifest: Manifest, worktree: Path
                       ) -> tuple[dict[str, str],
                                  tuple[dict[str, str], ...]]:
    """Rebuild each retained profile's own evidence for one source snapshot."""
    watch = sorted({entry for profile in manifest.profiles
                    for entry in profile.watch})
    ignore_files = ignore_rule_files(worktree)
    tracked_watch = {
        relative: relative in gitops.tracked_paths(Path(worktree), relative)
        for relative in watch
    }
    # Do not read an untracked local leftover merely because it has the same
    # bytes as a formerly tracked watch.  Tracking provenance is part of the
    # evidence, while the internal marker below never enters the manifest.
    digests: dict[str, str] = {}
    for relative in sorted(set(watch) | set(ignore_files)):
        if relative in tracked_watch and not tracked_watch[relative]:
            digests[relative] = _UNTRACKED
        else:
            digests[relative] = _digest_of(Path(worktree) / relative)
    snapshots = tuple({
            relative: digests[relative]
            for relative in set(profile.watch) | set(ignore_files)
            if relative in digests
        }
        for profile in manifest.profiles)
    return digests, snapshots


def changed_watch_evidence(profile: Profile,
                           current: dict[str, str]) -> tuple[str, ...]:
    """Name only the watched files whose state actually differs, and how."""
    changes: list[str] = []
    for relative in sorted(set(profile.digests) | set(current)):
        recorded = profile.digests.get(relative, ABSENT)
        now = current.get(relative, ABSENT)
        if now == recorded:
            continue
        if now == _UNTRACKED:
            changes.append(
                f"{relative} (no longer Git-tracked; choose a currently "
                f"tracked file with `{REVIEW_COMMAND}`)")
        elif recorded == ABSENT:
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


def _parent_problem(root: Path, relative: str) -> str:
    """Return a reason a relative path has an unsafe existing parent."""
    current = Path(root)
    parts = relative.split("/")[:-1]
    for part in parts:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            # All following parents are necessarily absent too; they can be
            # created below the real worktree root without traversing anything.
            break
        except OSError as e:
            return f"cannot inspect parent {current}: {e}"
        if pathops.is_link_stat(info) or pathops.is_reparse_point(info):
            return (f"parent {current} is a link or reparse point, so the "
                    "shared path cannot be proven to stay inside the worktree")
        if not stat.S_ISDIR(info.st_mode):
            return f"parent {current} is not an ordinary directory"
    return ""


def _is_ignored_directory(root: Path, relative: str) -> bool:
    """True when Git treats the existing directory as one ignored tree."""
    if gitops.is_path_ignored(root, relative, directory=True):
        return True
    expected = relative.rstrip("/") + "/"
    return expected in gitops.ignored_entries(root)


def target_problem(main: Path, relative: str) -> str:
    """Why the primary worktree cannot serve ``relative`` as a shared target.

    An empty string means it can: the path is an ordinary directory there (not a
    link, not a file, not a reparse point assent cannot classify) and Git ignores
    it, so linking to it neither shadows tracked content nor exports anything
    Git is supposed to carry itself.
    """
    main = Path(main)
    path = main / relative
    parent_problem = _parent_problem(main, relative)
    if parent_problem:
        return f"{relative} has an unsafe parent in the primary worktree: {parent_problem}"
    if not os.path.lexists(path):
        return f"{relative} does not exist in the primary worktree {main}"
    if not _is_ordinary_directory(path):
        return (f"{relative} is not an ordinary directory in the primary "
                f"worktree {main}")
    try:
        if not _is_ignored_directory(main, relative):
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
    if not _is_ordinary_directory(root):
        raise AssentError(
            f"refusing to snapshot shared target {root}: it is no longer an "
            "ordinary primary-worktree directory")
    digest = hashlib.sha256()
    digest.update(b"assent-shared-target-v1\n")
    digest.update(relative.encode("utf-8"))
    digest.update(b"\n")
    pending = [("", root)]
    while pending:
        prefix, current = pending.pop()
        try:
            info = os.lstat(current)
        except OSError as e:
            raise AssentError(
                f"Unable to inspect the shared target directory {current}: {e}") from e
        if (pathops.is_link_stat(info) or pathops.is_reparse_point(info)
                or not stat.S_ISDIR(info.st_mode)):
            raise AssentError(
                f"refusing to snapshot the shared target directory {current}: "
                "it changed to a link, reparse point, or non-directory")
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

    It covers, in the caller's own contributing order, each source's plan name
    and selected profile fingerprint, that profile's normalized declared paths,
    the exact resolved primary-worktree target of each one, and a content
    snapshot of that target.  REVIEWED-NONE contributes an explicit empty-profile
    line, so "reviewed to need nothing" is evidence and is never confused with
    UNKNOWN, which has no digest at all because it may not reach a receipt.
    """
    digest = hashlib.sha256()
    digest.update(b"assent-shared-inputs-v1\n")
    snapshots: dict[str, str] = {}
    for plan_name, contract in contracts:
        if not contract.settled:
            raise AssentError(
                f"refusing to record shared-input evidence for {plan_name}: its "
                f"shared-path contract is {contract.state}, not a reviewed answer")
        fingerprint = contract.profile.fingerprint if contract.profile else ""
        digest.update(f"{plan_name}\0{contract.state}\0{fingerprint}\n"
                      .encode("utf-8"))
        for relative in contract.paths:
            problem = target_problem(main, relative)
            if problem:
                raise AssentError(
                    f"refusing to record shared-input evidence for {plan_name}: "
                    f"{problem}")
            if relative not in snapshots:
                snapshots[relative] = snapshot_target(main, relative)
            digest.update(
                f"{relative}\0{_link_target(main, relative).as_posix()}\0"
                f"{snapshots[relative]}\n".encode("utf-8"))
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
def ignored_inventory(worktree: Path) -> tuple[str, ...]:
    """List collapsed ordinary ignored directories without walking them."""
    root = Path(worktree)
    found: list[str] = []
    for raw in gitops.ignored_entries(root):
        if not raw.endswith("/"):
            continue
        candidate = raw.replace("\\", "/").rstrip("/")
        if candidate.split("/")[0] in _EXCLUDED_ROOTS:
            continue
        relative = require_safe_relative(candidate, "ignored inventory")
        path = root / relative
        try:
            info = os.lstat(path)
        except OSError as e:
            raise AssentError(
                f"Unable to inspect ignored inventory entry {path}: {e}") from e
        if (not pathops.is_link_stat(info)
                and not pathops.is_reparse_point(info)
                and stat.S_ISDIR(info.st_mode)):
            found.append(relative)
    return tuple(sorted(set(found)))


def ignored_directory_candidates(worktree: Path) -> tuple[str, ...]:
    """Return the ordinary ignored directories eligible for review."""
    return ignored_inventory(worktree)


def has_ignored_directory_candidate(worktree: Path) -> bool:
    """True when the worktree really holds an ordinary ignored directory."""
    return bool(ignored_directory_candidates(worktree))


def changed_inventory_evidence(
        profile: Profile, current: tuple[str, ...]) -> tuple[str, ...]:
    """Describe ignored-directory inventory membership changes."""
    recorded = set(profile.inventory)
    now = set(current)
    changes: list[str] = []
    for relative in sorted(recorded | now):
        if relative not in recorded:
            changes.append(f"ignored directory added: {relative}")
        elif relative not in now:
            changes.append(f"ignored directory removed: {relative}")
    return tuple(changes)


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

    Classification is state-only. Every consumer must separately require that
    the source's directory links agree with a settled answer; provisioning
    callers may allow missing declared links only for the duration of their
    locked reconcile step.
    """
    main = Path(main)
    worktree = Path(worktree)
    manifest = read_manifest(main) if manifest is None else manifest
    inventory = ignored_inventory(main)

    # One tracked-ignore query serves every retained profile.  Each profile is
    # then compared with its own watch set plus this exact current ignore-rule
    # set; the union of all profiles' watches must not make otherwise reusable
    # branch fingerprints stale merely because another branch watches more.
    digests, profile_digests = _profile_snapshots(manifest, worktree)
    matches = tuple(
        profile for profile, current in zip(manifest.profiles, profile_digests)
        if _matches(profile, current, inventory=inventory))

    if len({profile.paths for profile in matches}) > 1:
        listed = "; ".join(
            f"{profile.fingerprint[:12]} -> {list(profile.paths)}"
            for profile in matches)
        raise AssentError(
            f"the shared-path manifest {manifest_path(main)} holds conflicting "
            f"matching profiles ({listed}); resolve them with "
            f"`{REVIEW_COMMAND}` before any session runs")

    if not matches:
        # Inventory drift can be caused by a previously declared target itself
        # disappearing or becoming tracked. Report that concrete target failure
        # before the more general inventory change so the operator gets the
        # existing actionable refusal instead of an unnecessary semantic review.
        active = next((
            (profile, current)
            for profile, current in reversed(tuple(zip(
                manifest.profiles, profile_digests)))
            if _evidence_matches(profile, current)), None)
        if active is not None:
            active_profile, active_digests = active
            problems = tuple(
                problem for problem in (
                    target_problem(main, relative)
                    for relative in active_profile.paths)
                if problem)
            if problems:
                return Contract(
                    STALE, active_profile, active_digests,
                    active_profile.paths, problems, needs_review=True,
                    inventory=inventory)
        prior = manifest.profiles[-1].paths if manifest.profiles else ()
        evidence = tuple(
            change for profile, current in zip(
                manifest.profiles, profile_digests)
            for change in (
                changed_watch_evidence(profile, current)
                + changed_inventory_evidence(profile, inventory)
                + (("shared-path review context upgraded; reconsider the "
                    "complete primary ignored-directory inventory",)
                   if (profile.review_version != REVIEW_CONTEXT_VERSION
                       and _evidence_matches(profile, current)) else ())))
        # Verifier evidence naming a required ignored directory is a real
        # subject on its own: it must never settle as "there is nothing to
        # declare", and a directory the primary worktree cannot serve is
        # refused with that exact problem instead of being queued for a review
        # that could not succeed.
        required = _required_evidence_paths(main, required_evidence)
        if manifest.profiles:
            state = STALE
        elif required or inventory:
            state = UNKNOWN
        else:
            state = NO_IGNORED_DIRECTORY_CANDIDATE
        evidence += tuple(_evidence_note(relative) for relative in required)
        return Contract(
            state, None, digests, prior, tuple(dict.fromkeys(evidence)),
            needs_review=state in (STALE, UNKNOWN), inventory=inventory)

    profile = matches[0]
    profile_index = manifest.profiles.index(profile)
    digests = profile_digests[profile_index]
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
                        needs_review=True,
                        inventory=inventory)
    return Contract(
        REVIEWED_NONE if profile.is_none else REVIEWED_PATHS,
        profile, digests, profile.paths, inventory=inventory)


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


def ignored_directory_links(worktree: Path) -> tuple[str, ...]:
    """List ignored directory-link objects without entering their targets."""
    root = Path(worktree)
    found: list[str] = []
    for entry in gitops.ignored_entries(root):
        if not entry.endswith("/"):
            continue
        relative = entry.rstrip("/")
        if relative.split("/")[0] in _EXCLUDED_ROOTS:
            continue
        require_safe_relative(relative, root)
        if pathops.is_link(root / relative):
            found.append(relative)
    return tuple(sorted(found))


def review_contract_with_source_links(
        main: Path, worktree: Path, contract: Contract) -> Contract:
    """Expose reviewable same-primary orphan links to the next session.

    A settled profile remains authoritative unless an existing ignored link is
    already the exact same-relative link Assent would provision from the
    primary worktree.  Such a link is safe evidence for another bounded review:
    it is left untouched, named in the prompt, and makes an otherwise settled
    answer STALE.  Foreign links and unusable primary targets keep the ordinary
    fail-closed agreement error instead of being presented as reviewable.
    """
    main = Path(main)
    worktree = Path(worktree)
    unexpected = sorted(
        set(ignored_directory_links(worktree)) - set(contract.paths))
    reviewable = tuple(
        relative for relative in unexpected
        if not target_problem(main, relative)
        and _resolves_to(worktree / relative, _link_target(main, relative)))
    if not reviewable:
        return contract
    evidence = contract.evidence + tuple(
        f"source worktree has an unreviewed same-primary directory link: {relative}"
        for relative in reviewable
        if not any(relative in item for item in contract.evidence))
    if contract.settled:
        return Contract(
            STALE, contract.profile, contract.digests, contract.paths,
            evidence, needs_review=True,
            inventory=ignored_inventory(main))
    return replace(contract, evidence=evidence)


def require_directory_link_agreement(
        main: Path, worktree: Path, contract: Contract, *,
        plan_name: str | None = None, allow_missing: bool = False) -> None:
    """Require source directory links to reproduce exactly one reviewed answer.

    Git supplies a collapsed ignored-entry inventory and each link object is
    inspected only at its own path. Unexpected links are never resolved: an
    unreviewed external target is neither enumerated nor hashed for diagnosis.
    """
    root = Path(worktree)
    plan_name = plan_name or root.name
    declared = set(contract.paths)
    actual = set(ignored_directory_links(root))
    unexpected = sorted(actual - declared)
    if unexpected:
        relative = unexpected[0]
        raise AssentError(
            f"refusing to rely on shared inputs for {plan_name}: source worktree "
            f"{root} contains the ignored directory link {relative}, which is "
            f"outside its active {contract.state} profile. Remove the link if "
            "it is irrelevant. If it is required, place its ordinary "
            f"Git-ignored target at {Path(main) / relative} and record "
            f"{relative} with `{REVIEW_COMMAND}`; an external hand-provisioned "
            "link is not reviewed evidence")
    if Path(main).resolve() == root.resolve():
        # A vanished source falls back to the primary snapshot. Its declared
        # targets are the ordinary directories themselves, never links to self.
        return
    for relative in contract.paths:
        destination = root / relative
        if not os.path.lexists(destination):
            if allow_missing:
                continue
            raise AssentError(
                f"refusing to rely on shared inputs for {plan_name}: the active "
                f"profile declares {relative}, but {destination} is missing. "
                "Reconcile the source so Assent can provision the exact "
                "same-relative primary-worktree link")
        target = _link_target(main, relative)
        if relative not in actual or not _resolves_to(destination, target):
            raise AssentError(
                f"refusing to rely on shared inputs for {plan_name}: the active "
                f"profile declares {relative}, but {destination} is not a "
                f"directory link to the reviewed primary target {target}. "
                "Remove an irrelevant link; otherwise place the required "
                "ordinary Git-ignored target at that primary path and record "
                f"it with `{REVIEW_COMMAND}`")


def _validate_destination(main: Path, worktree: Path,
                          relative: str) -> Path:
    """Validate one destination without changing either manifest or filesystem."""
    problem = target_problem(main, relative)
    if problem:
        raise AssentError(
            f"refusing to provision shared paths for {worktree}: {problem}")
    parent_problem = _parent_problem(worktree, relative)
    if parent_problem:
        raise AssentError(
            f"refusing to provision the shared path {relative} into {worktree}: "
            f"{parent_problem}")
    destination = Path(worktree) / relative
    target = _link_target(main, relative)
    if os.path.lexists(destination):
        if not _resolves_to(destination, target):
            raise AssentError(
                f"refusing to provision the shared path {relative} into {worktree}: "
                f"{destination} already exists and is not a link to {target}")
        if not _is_ignored_directory(Path(worktree), relative):
            raise AssentError(
                f"refusing to provision the shared path {relative} into {worktree}: "
                "Git does not ignore the existing directory link there")
    if gitops.tracked_paths(Path(worktree), relative):
        raise AssentError(
            f"refusing to provision the shared path {relative} into {worktree}: "
            "tracked content lives there and a shared link must never shadow it")
    return target


def _provision_one(main: Path, worktree: Path, relative: str) -> bool:
    """Ensure one exact link exists; return whether this call created it."""
    target = _validate_destination(main, worktree, relative)
    destination = Path(worktree) / relative
    if os.path.lexists(destination):
        return False
    parent = destination.parent
    if not parent.exists():
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
    try:
        if not _is_ignored_directory(Path(worktree), relative):
            raise AssentError(
                f"refusing to provision the shared path {relative} into {worktree}: "
                "Git does not ignore the linked directory there, so the link "
                "would change what the worktree tracks")
    except BaseException as primary_error:
        try:
            pathops.detach_directory_link(destination)
        except OSError as cleanup_error:
            primary_error.add_note(
                f"Unable to detach the rejected shared-path link {destination}: "
                f"{cleanup_error}")
        raise
    return True


def _detach_one(main: Path, worktree: Path, relative: str) -> bool:
    """Detach one link assent created; anything else is left exactly as found.

    The proof required is deliberately narrow: the place must still hold a link
    object that resolves to the primary worktree's same relative directory.  An
    ordinary directory, a foreign link and a missing path are all no-ops, and
    the target itself is never walked, modified or removed.
    """
    destination = Path(worktree) / relative
    if not os.path.lexists(destination):
        return False
    parent_problem = _parent_problem(worktree, relative)
    if parent_problem:
        raise AssentError(
            f"Unable to prove ownership of the shared-path link {destination}: "
            f"{parent_problem}")
    target_root = Path(main)
    primary_parent_problem = _parent_problem(target_root, relative)
    if primary_parent_problem:
        raise AssentError(
            f"Unable to prove ownership of the shared-path link {destination}: "
            f"the primary target has an unsafe parent ({primary_parent_problem})")
    primary_target = target_root / relative
    if not os.path.lexists(primary_target) or not _is_ordinary_directory(
            primary_target):
        raise AssentError(
            f"Unable to prove ownership of the shared-path link {destination}: "
            f"the primary target {primary_target} is no longer an ordinary "
            "directory")
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
              manifest: Manifest | None = None,
              force_write: bool = False) -> tuple[tuple[str, ...],
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
    manifest = read_manifest(main) if manifest is None else manifest
    if _worktree_key(worktree) == _worktree_key(main):
        if force_write:
            write_manifest(main, manifest)
        return (), ()

    wanted = contract.paths
    previous = applied_paths(manifest, worktree)

    # Validate every destination before creating any link.  This is the
    # ownership boundary for review/provision: a later collision must not leave
    # an earlier new link behind while the old manifest still describes the old
    # application.
    for relative in wanted:
        _validate_destination(main, worktree, relative)

    original_applications = manifest.applications
    created: list[str] = []
    detached: list[str] = []
    try:
        for relative in wanted:
            if _provision_one(main, worktree, relative):
                created.append(relative)
        for relative in previous:
            if relative not in wanted and _detach_one(main, worktree, relative):
                detached.append(relative)
        changed = _record_application(
            manifest, worktree,
            contract.profile.fingerprint if contract.profile else "", wanted)
        if changed or force_write:
            write_manifest(main, manifest)
    except BaseException as primary_error:
        # No manifest/application update is considered complete until all link
        # operations and the atomic manifest replacement have succeeded.  Roll
        # back only links proven to belong to this invocation; foreign links,
        # ordinary directories, and uncertain targets are retained and named.
        rollback_problems: list[str] = []
        for relative in reversed(created):
            try:
                _detach_one(main, worktree, relative)
            except AssentError as e:
                rollback_problems.append(str(e))
        for relative in reversed(detached):
            destination = Path(worktree) / relative
            try:
                if not os.path.lexists(destination):
                    _provision_one(main, worktree, relative)
                elif not _resolves_to(destination, _link_target(main, relative)):
                    rollback_problems.append(
                        f"unable to restore the prior shared-path link {destination}: "
                        "the destination changed while rolling back")
            except (AssentError, OSError) as e:
                rollback_problems.append(
                    f"unable to restore the prior shared-path link {destination}: {e}")
        manifest.applications = original_applications
        if rollback_problems:
            primary_error.add_note(
                "Shared-path rollback was incomplete: "
                + "; ".join(rollback_problems))
        raise
    return tuple(created), tuple(detached)


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
    same_fingerprint = tuple(
        profile for profile in manifest.profiles
        if profile.fingerprint == record.fingerprint)
    if not same_fingerprint:
        return (f"the shared-path profile {record.fingerprint[:12]} recorded for "
                f"{worktree} is no longer in {manifest_path(main)}")
    if not any(profile.paths == record.paths for profile in same_fingerprint):
        current = "; ".join(
            f"{list(profile.paths)}" for profile in same_fingerprint)
        return (f"the shared-path application for {worktree} recorded paths "
                f"{list(record.paths)} under profile {record.fingerprint[:12]}, "
                f"but the current profile declares {current}")
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
    plan, exact selected batch, dynamic batch, localization prefix, and
    ``--focus``.  Each contributing live source worktree is
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
        for plan_name, worktree in sources:
            # A plan whose source worktree is gone has no snapshot of its own,
            # so it is classified against the primary worktree -- the same
            # receipt-backed fallback acceptance already uses.  Doing it here
            # rather than skipping keeps a later freshness check comparable.
            contract = classify(main, worktree or main, manifest)
            if not contract.settled:
                raise AssentError(
                    f"refusing to verify: the shared-path contract for "
                    f"{plan_name} ({worktree}) is {contract.state}. "
                    f"{closeout_refusal(contract) or 'Run `' + REVIEW_COMMAND + '`'}")
            require_directory_link_agreement(
                main, worktree or main, contract, plan_name=plan_name,
                allow_missing=True)
            reconcile(main, worktree or main, contract, manifest=manifest)
            require_directory_link_agreement(
                main, worktree or main, contract, plan_name=plan_name)
            prepared.append((plan_name, contract))
    return tuple(prepared)


def prepare_worktree(main: Path, worktree: Path, *,
                     required_evidence: Iterable[str] = ()) -> Contract:
    """Classify a source worktree and, when the answer is settled, apply it.

    This is what runs before a scheduled task session: REVIEWED-PATHS provisions
    every declared missing link, REVIEWED-NONE starts with no links and no extra
    AI instructions, and UNKNOWN or STALE touches the filesystem not at all and
    leaves the session to run the controlled review. A same-primary orphan link
    is surfaced as STALE evidence rather than making a reusable worktree
    permanently refuse before that review can start.
    """
    # Classification and application must observe one manifest generation.  A
    # review running between these two operations must either serialize before
    # this section or wait until both facts have been applied; it may not replace
    # the selected profile after classification and before provisioning.
    with hold_manifest_lock(main):
        manifest = read_manifest(main)
        contract = classify(
            main, worktree, manifest, required_evidence=required_evidence)
        contract = review_contract_with_source_links(
            main, worktree, contract)
        if contract.settled:
            require_directory_link_agreement(
                main, worktree, contract, allow_missing=True)
            reconcile(main, worktree, contract, manifest=manifest)
            require_directory_link_agreement(main, worktree, contract)
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


def _validate_dispositions(
        dispositions: Sequence[PathDisposition]) -> tuple[PathDisposition, ...]:
    normalized: list[PathDisposition] = []
    for index, item in enumerate(dispositions):
        if not isinstance(item, PathDisposition):
            raise AssentError(
                f"shared-path disposition {index} must contain path and reason")
        relative = require_safe_relative(item.path, "shared-path disposition")
        reason = item.reason.strip() if isinstance(item.reason, str) else ""
        if not reason:
            raise AssentError(f"disposition for {relative} needs a non-empty reason")
        normalized.append(PathDisposition(relative, reason))
    if len({item.path for item in normalized}) != len(normalized):
        raise AssentError("shared-path dispositions contain a duplicate path")
    for item in normalized:
        if any(other.path != item.path
               and item.path.startswith(f"{other.path}/")
               for other in normalized):
            raise AssentError(
                "shared-path dispositions contain overlapping paths")
    return tuple(sorted(normalized, key=lambda item: item.path))


def validate_review_decision(
        main: Path, worktree: Path, paths: Sequence[str],
        watch: Sequence[str],
        dispositions: Sequence[PathDisposition] = ()) -> ValidatedReview:
    """Validate one structured reviewer decision without mutating anything."""
    declared = _validate_paths(Path(main), paths) if paths else ()
    watched = _validate_watch(Path(worktree), watch)
    inventory = ignored_inventory(Path(main))
    classified = _validate_dispositions(dispositions)
    inventory_paths = set(inventory)
    shared = set(declared)
    disposition_roots = {item.path for item in classified}
    covered_shared = {
        relative for relative in inventory_paths
        if any(relative == root or relative.startswith(f"{root}/")
               for root in shared)}
    covered_classified = {
        relative for relative in inventory_paths
        if any(relative == root or relative.startswith(f"{root}/")
               for root in disposition_roots)}
    empty_shared = sorted(
        root for root in shared
        if not any(relative == root or relative.startswith(f"{root}/")
                   for relative in inventory_paths))
    empty_dispositions = sorted(
        root for root in disposition_roots
        if not any(relative == root or relative.startswith(f"{root}/")
                   for relative in inventory_paths))
    overlap = sorted(covered_shared & covered_classified)
    missing = sorted(
        inventory_paths - covered_shared - covered_classified)
    if empty_shared or empty_dispositions or overlap or missing:
        details: list[str] = []
        if empty_shared:
            details.append("shared but covers no inventory directory: "
                           + ", ".join(empty_shared))
        if overlap:
            details.append("both shared and classified: " + ", ".join(overlap))
        if missing:
            details.append("unclassified: " + ", ".join(missing))
        if empty_dispositions:
            details.append("disposition covers no inventory directory: "
                           + ", ".join(empty_dispositions))
        raise AssentError(
            "shared-path review must cover the complete primary ignored-directory "
            "inventory (" + "; ".join(details) + ")")
    unexpected = sorted(
        set(ignored_directory_links(Path(worktree))) - set(declared))
    manifest = read_manifest(Path(main))
    recorded = set(applied_paths(manifest, Path(worktree)))
    foreign = [
        relative for relative in unexpected
        if relative not in recorded
        or not _resolves_to(
            Path(worktree) / relative, _link_target(Path(main), relative))]
    if foreign:
        raise AssentError(
            "shared_paths.paths omits existing ignored directory link(s): "
            + ", ".join(foreign)
            + ". Inspect their tracked build declarations and return a corrected "
              "shared_paths object that includes every required link; do not run "
              "the shared-paths CLI")
    if Path(main).resolve() != Path(worktree).resolve():
        for relative in declared:
            _validate_destination(Path(main), Path(worktree), relative)
    return ValidatedReview(declared, watched, inventory, classified)


def review(main: Path, worktree: Path, *,
           paths: Sequence[str] = (), watch: Sequence[str] = (),
           none: bool = False,
           dispositions: Sequence[PathDisposition] = ()) -> Contract:
    """Record one reviewed shared-path profile, then reconcile this worktree.

    Every value is validated before anything is mutated: a path must be an
    existing ordinary Git-ignored directory at the same relative place in the
    primary worktree, and a watch must be a readable tracked file in the source
    snapshot.  The whole update happens under one project-local lock and lands
    through one atomic replacement, so a concurrent attempt is refused rather
    than interleaved and an interruption leaves the previous complete file.

    Recording a profile replaces every retained answer that matches this exact
    source snapshot, even when those answers watched different files. Profiles
    for genuinely different snapshots remain cached for later branch switches.
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

    with hold_manifest_lock(main):
        decision = validate_review_decision(
            main, worktree, () if none else paths, watch, dispositions)
        declared = decision.paths
        watched = decision.watch
        manifest = read_manifest(main)
        _all_digests, current = _profile_snapshots(manifest, worktree)
        digests = snapshot_digests(worktree, watched)
        profile = Profile(
            fingerprint_of(digests), declared, watched, digests,
            REVIEW_CONTEXT_VERSION, decision.inventory,
            decision.dispositions)
        manifest.profiles = tuple(
            existing for existing, snapshot in zip(manifest.profiles, current)
            if not _matches(existing, snapshot)
            and existing.fingerprint != profile.fingerprint) + (profile,)
        manifest.version = SCHEMA_VERSION

        contract = Contract(REVIEWED_NONE if profile.is_none else REVIEWED_PATHS,
                            profile, digests, profile.paths,
                            inventory=decision.inventory)
        # The prospective profile is held only in memory until every declared
        # destination has passed preflight and every required link has been
        # reconciled.  ``force_write`` also covers REVIEWED-NONE and the primary
        # worktree, where there is no application-record change to trigger a
        # manifest write.
        reconcile(main, worktree, contract, manifest=manifest,
                  force_write=True)
    return contract


# --------------------------------------------------------------------------- #
# What a session and the scheduler are told
# --------------------------------------------------------------------------- #
_REVIEW_CLAUSE = (
    "\nShared ignored directories for this repository are {state}. Before you "
    "close this task out you must run, in this worktree:\n"
    "  {command} --path DIR --classify PATH REASON --watch FILE\n"
    "  {command} --none --classify PATH REASON --watch FILE\n"
    "Repeat --path, --classify, and --watch as needed. Every inventory directory "
    "must be covered exactly once by either --path or --classify; a disposition "
    "may cover its subtree and needs a concrete reason.\n"
    "Decide from the repository's Git-ignore rules, its dependency/build "
    "declarations and this task's verifier evidence alone -- do not audit the "
    "whole repository. Name as --watch exactly the tracked dependency or build "
    "files that would make this decision worth reconsidering.{prior}{evidence}\n"
    "{candidates}\n"
    "The scheduler refuses this task's completion while the shared-path "
    "contract is still unreviewed.")

_REVIEW_RECORD_CLAUSE = (
    "\nShared ignored directories for this repository are {state}. Decide from "
    "the repository's Git-ignore rules, its dependency/build declarations and "
    "this review's verifier evidence alone -- do not audit the whole repository. "
    "Do not run `{command}` in this plan-review session. Instead, the terminal "
    "assent.auto_fix_review JSON must set `shared_paths` to an object with exact "
    "`paths`, `dispositions`, and `watch` lists. Each disposition object has "
    "exact `path` and non-empty `reason` fields. Every inventory directory must "
    "be covered exactly once by paths or dispositions; a disposition may cover "
    "its subtree. Use an empty `paths` list "
    "when no shared directory is required. Name in `watch` exactly the tracked dependency or "
    "build files that would make this decision worth reconsidering. The scheduler "
    "validates and applies the decision before accepting the verdict.{prior}{evidence}\n"
    "{candidates}")


def _candidate_inventory(contract: Contract) -> str:
    """Format the complete primary inventory without claiming it is required."""
    heading = (
        "Complete primary worktree ignored-directory inventory (presence alone "
        "does not mean a directory should be shared):")
    if not contract.inventory:
        return heading + "\n  (none)"
    return heading + "\n" + "\n".join(
        f"  - {relative}" for relative in contract.inventory)


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
                                 prior=prior, evidence=evidence,
                                 candidates=_candidate_inventory(contract))


def review_record_clause(contract: Contract) -> str:
    """Require a structured decision from a plan reviewer when needed."""
    if contract.settled or not contract.needs_review:
        return ""
    prior = ("\nPreviously reviewed shared paths: "
             + ", ".join(contract.prior_paths) if contract.prior_paths else "")
    evidence = ("\nWhat changed: " + "; ".join(contract.evidence)
                if contract.evidence else "")
    return _REVIEW_RECORD_CLAUSE.format(
        state=contract.state, command=REVIEW_COMMAND,
        prior=prior, evidence=evidence,
        candidates=_candidate_inventory(contract))


def closeout_refusal(contract: Contract) -> str:
    """The precise retry reason for a session that never settled the contract."""
    if contract.settled or not contract.needs_review:
        return ""
    evidence = f" ({'; '.join(contract.evidence)})" if contract.evidence else ""
    return (f"the shared-path contract for this source is still "
            f"{contract.state}{evidence}; run `{REVIEW_COMMAND}` with the "
            "reviewed --path/--none, --classify, and --watch values before "
            "closing out")


def describe(contract: Contract) -> str:
    """One operator-facing line stating the contract a session starts under."""
    if contract.state == REVIEWED_PATHS:
        return f"Shared paths: REVIEWED-PATHS ({', '.join(contract.paths)})"
    if contract.state == REVIEWED_NONE:
        return "Shared paths: REVIEWED-NONE (no shared directory is required)"
    if contract.state == NO_IGNORED_DIRECTORY_CANDIDATE:
        return (f"Shared paths: {NO_IGNORED_DIRECTORY_CANDIDATE} (the primary "
                "worktree holds no ordinary ignored directory anyone could share)")
    return f"Shared paths: {contract.state}; one bounded review is required"
