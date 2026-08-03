"""Provider-neutral review records and durable state for folder auto-fix.

This module owns data validation and persistence only.  Adapters remain
responsible for running vendor CLIs, while later scheduler code can consume the
validated records without trusting reviewer-chosen identities.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Iterable

from assent import AssentError, pathops
from assent.config import Config
from assent.verification_common import (DIGEST_RE, OID_RE, atomic_write_text,
                                        toml_string)

if TYPE_CHECKING:
    from assent.plan import Plan

AUTO_FIX_STATE_NAME = "_auto_fix.toml"
AUTO_FIX_STATE_VERSION = 5
REVIEW_RECORD_TYPE = "assent.auto_fix_review"
REVIEW_VERDICTS = frozenset({"PASS", "FAIL"})
REVIEW_FINDING_KINDS = frozenset({
    "correctness", "safety", "unmet_requirement", "focused_test_gap",
    "eligible_technical_debt", "blocked_recovery", "scope_amendment",
})
REVIEW_TRANSITION_KINDS = frozenset({
    "initial", "still_present", "repair_regression", "newly_exposed",
})
SCOPE_PATH_STATES = frozenset({"existing_file", "new_file"})
REVIEW_CONTEXTS = frozenset({"completed_folder", "blocked_adjudication"})
REVIEW_STAGES = frozenset({"initial", "recheck"})
FAILURE_TRIGGERS = frozenset({"worker_blocked", "focused_gate_failure"})
REPAIR_DISPOSITIONS = frozenset({
    "fixed", "not_reproducible", "still_blocked",
})
REPAIR_DISPOSITION_PREFIX = "ASSENT_REPAIR_DISPOSITION "
AUTO_FIX_PHASES = frozenset({
    "NEEDS_REPAIR", "REPAIRING", "AWAITING_REVIEW", "COMPLETE",
})
# Basenames no reviewed scope amendment may authorize, at any directory depth:
# the instruction and Git-control files that govern how a session behaves, and
# Assent's own derived management artifacts.
_PROTECTED_SCOPE_BASENAMES = frozenset({
    "agents.md", ".gitignore", ".gitattributes", ".gitmodules",
    "_auto_fix.toml", "_verification.toml", "_batch_verification.toml",
    "_archived.toml", "_report.md", "assent.lock",
})

MAX_REVIEW_OUTPUT_BYTES = 1_048_576
MAX_REVIEW_RECORD_BYTES = 262_144
MAX_FINDINGS = 100
MAX_PATH_LENGTH = 1024
MAX_SUMMARY_LENGTH = 500
MAX_EVIDENCE_LENGTH = 16_000
MAX_RECOMMENDATION_LENGTH = 4_000
_TASK_ID_RE = re.compile(r"^t\d{3}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_REVIEW_KEYS = {"type", "verdict", "findings"}
_FINDING_KEYS = {
    "kind", "task_id", "path", "summary", "evidence", "recommendation",
    "scope_addition", "transition", "prior_fingerprint",
    "transition_evidence",
}
_SCOPE_ADDITION_KEYS = {"path", "path_state"}
_STATE_KEYS = {
    "version", "source_tree", "task_plan_sha256", "review_prompt_sha256",
    "reviewer_adapter", "reviewer_model", "reviewer_effort", "phase", "verdict",
    "review_context", "review_stage", "failure_trigger",
    "current_finding_fingerprints", "findings", "observed_states",
    "reviewer_recommendations", "approved_scope_additions",
    "scope_amendments", "worker_dispositions", "repair_briefs",
    "repair_round_assignments", "plan_digest_transitions",
    "review_transitions", "consumed_fixer_profiles",
}
_PERSISTED_FINDING_KEYS = {
    "fingerprint", "kind", "task_id", "path", "summary", "evidence",
    "recommendation", "scope_addition_path", "scope_addition_path_state",
}
_OBSERVED_STATE_KEYS = {"source_tree", "finding_fingerprints"}
_FIXER_PROFILE_KEYS = {"adapter", "model", "effort"}
_RECOMMENDATION_KEYS = {"fingerprint", "recommendation"}
_APPROVED_SCOPE_ADDITION_KEYS = {
    "fingerprint", "task_id", "path", "path_state",
}
_SCOPE_AMENDMENT_KEYS = {
    "finding_fingerprints", "task_id", "paths", "path_states",
    "task_before_sha256", "task_after_sha256", "plan_before_sha256",
    "plan_after_sha256",
}
_WORKER_DISPOSITION_KEYS = {
    "task_id", "fingerprint", "disposition", "detail",
}
_REPAIR_BRIEF_KEYS = {"task_id", "finding_fingerprints", "brief"}
_REPAIR_ROUND_ASSIGNMENT_KEYS = {
    "task_id", "adapter", "model", "effort", "attempted",
}
_PLAN_DIGEST_TRANSITION_KEYS = {"before_sha256", "after_sha256"}
_REVIEW_TRANSITION_KEYS = {
    "fingerprint", "transition", "prior_fingerprint", "transition_evidence",
}


@dataclass(frozen=True)
class ScopeAddition:
    """One exact project-relative task-scope amendment proposed by review."""

    path: str
    path_state: str


def review_record_schema() -> dict:
    """Return Codex's deliberately limited structured-review transport schema.

    This is a transport aid for the Codex adapter, not Assent's portable review contract.
    Other adapters keep their provider-neutral raw-output fallback unless they independently
    implement and test a native structured-output dialect.  Every adapter's response still
    passes the strict parser and scheduler validator after transport, including task ownership
    and declared scope.
    """
    text_schema = {
        "type": "string",
        "minLength": 1,
        "pattern": r"^[^\u0000-\u001f\u007f]+$",
    }
    finding_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_id", "path", "summary", "evidence"],
        "properties": {
            "task_id": {
                "type": ["string", "null"],
                "pattern": r"^t\d{3}$",
            },
            "path": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_PATH_LENGTH,
            },
            "summary": {**text_schema, "maxLength": MAX_SUMMARY_LENGTH},
            "evidence": {**text_schema, "maxLength": MAX_EVIDENCE_LENGTH},
        },
    }
    findings_schema = {
        "type": "array",
        "maxItems": MAX_FINDINGS,
        "items": finding_schema,
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["type", "verdict", "findings"],
        "properties": {
            "type": {"type": "string", "enum": [REVIEW_RECORD_TYPE]},
            "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
            "findings": findings_schema,
        },
    }


@dataclass(frozen=True)
class ReviewFinding:
    """One blocking issue stated by a reviewer, before scheduler fingerprinting."""

    task_id: str | None
    path: str
    summary: str
    evidence: str
    kind: str = "correctness"
    recommendation: str = "Repair the finding and run the focused task gate."
    scope_addition: ScopeAddition | None = None
    transition: str = "initial"
    prior_fingerprint: str | None = None
    transition_evidence: str | None = None


@dataclass(frozen=True)
class ReviewRecord:
    """The one terminal, provider-neutral review verdict."""

    verdict: str
    findings: tuple[ReviewFinding, ...]

    def __post_init__(self) -> None:
        if isinstance(self.findings, list):
            object.__setattr__(self, "findings", tuple(self.findings))


@dataclass(frozen=True)
class PersistedFinding:
    """A normalized finding plus its scheduler-computed identity."""

    fingerprint: str
    kind: str
    task_id: str | None
    path: str
    summary: str
    evidence: str
    recommendation: str
    scope_addition_path: str | None
    scope_addition_path_state: str | None

    @property
    def finding(self) -> ReviewFinding:
        scope_addition = None
        if self.scope_addition_path is not None:
            scope_addition = ScopeAddition(
                self.scope_addition_path, self.scope_addition_path_state or "")
        return ReviewFinding(
            self.task_id, self.path, self.summary, self.evidence,
            kind=self.kind, recommendation=self.recommendation,
            scope_addition=scope_addition)


@dataclass(frozen=True)
class ObservedState:
    """A source tree and the exact blocking findings observed on that tree."""

    source_tree: str
    finding_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.finding_fingerprints, list):
            object.__setattr__(self, "finding_fingerprints",
                               tuple(self.finding_fingerprints))


@dataclass(frozen=True)
class FixerProfile:
    """One abstract adapter/model/effort repair profile already consumed."""

    adapter: str
    model: str
    effort: str


@dataclass(frozen=True)
class ReviewerRecommendation:
    fingerprint: str
    recommendation: str


@dataclass(frozen=True)
class ApprovedScopeAddition:
    fingerprint: str
    task_id: str
    path: str
    path_state: str


@dataclass(frozen=True)
class ScopeAmendment:
    """One precomputed, crash-resumable scheduler scope transaction."""

    finding_fingerprints: tuple[str, ...]
    task_id: str
    paths: tuple[str, ...]
    path_states: tuple[str, ...]
    task_before_sha256: str
    task_after_sha256: str
    plan_before_sha256: str
    plan_after_sha256: str

    def __post_init__(self) -> None:
        for name in ("finding_fingerprints", "paths", "path_states"):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))


@dataclass(frozen=True)
class WorkerDisposition:
    task_id: str
    fingerprint: str
    disposition: str
    detail: str


@dataclass(frozen=True)
class RepairBrief:
    task_id: str
    finding_fingerprints: tuple[str, ...]
    brief: str

    def __post_init__(self) -> None:
        if isinstance(self.finding_fingerprints, list):
            object.__setattr__(self, "finding_fingerprints",
                               tuple(self.finding_fingerprints))


@dataclass(frozen=True)
class RepairRoundAssignment:
    """One task/profile assignment durably selected before a repair round."""

    task_id: str
    adapter: str
    model: str
    effort: str
    attempted: bool

    @property
    def profile(self) -> FixerProfile:
        return FixerProfile(self.adapter, self.model, self.effort)


@dataclass(frozen=True)
class PlanDigestTransition:
    before_sha256: str
    after_sha256: str


@dataclass(frozen=True)
class ReviewTransition:
    fingerprint: str
    transition: str
    prior_fingerprint: str | None
    transition_evidence: str | None


@dataclass(frozen=True)
class AutoFixState:
    """Deletable runtime memory for one folder's bounded review/repair loop."""

    version: int
    source_tree: str
    task_plan_sha256: str
    review_prompt_sha256: str
    reviewer_adapter: str
    reviewer_model: str
    reviewer_effort: str
    phase: str
    verdict: str
    current_finding_fingerprints: tuple[str, ...]
    findings: tuple[PersistedFinding, ...]
    observed_states: tuple[ObservedState, ...]
    consumed_fixer_profiles: tuple[FixerProfile, ...]
    review_context: str = "completed_folder"
    review_stage: str = "initial"
    failure_trigger: str | None = None
    reviewer_recommendations: tuple[ReviewerRecommendation, ...] = ()
    approved_scope_additions: tuple[ApprovedScopeAddition, ...] = ()
    scope_amendments: tuple[ScopeAmendment, ...] = ()
    worker_dispositions: tuple[WorkerDisposition, ...] = ()
    repair_briefs: tuple[RepairBrief, ...] = ()
    repair_round_assignments: tuple[RepairRoundAssignment, ...] = ()
    plan_digest_transitions: tuple[PlanDigestTransition, ...] = ()
    review_transitions: tuple[ReviewTransition, ...] = ()

    def __post_init__(self) -> None:
        for name in ("current_finding_fingerprints", "findings",
                     "observed_states", "consumed_fixer_profiles",
                     "reviewer_recommendations", "approved_scope_additions",
                     "scope_amendments", "worker_dispositions", "repair_briefs",
                     "repair_round_assignments",
                     "plan_digest_transitions", "review_transitions"):
            value = getattr(self, name)
            if isinstance(value, list):
                object.__setattr__(self, name, tuple(value))

    @property
    def finding_ledger(self) -> tuple[PersistedFinding, ...]:
        return self.findings

    @property
    def review_adapter(self) -> str:
        return self.reviewer_adapter

    @property
    def review_model(self) -> str:
        return self.reviewer_model

    @property
    def review_effort(self) -> str:
        return self.reviewer_effort


@dataclass(frozen=True)
class ProjectSurfaceSnapshot:
    """Content identities for the source tree and Assent management plane.

    Directory links and Windows reparse points are recorded as leaf objects and
    never traversed.  This is a write detector around a cooperative read-only
    reviewer, not a process sandbox.
    """

    entries: tuple[tuple[str, str], ...]

    def changed_paths(self, other: "ProjectSurfaceSnapshot") -> tuple[str, ...]:
        before = dict(self.entries)
        after = dict(other.entries)
        return tuple(sorted(
            path for path in set(before) | set(after)
            if before.get(path) != after.get(path)))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _surface_entries(root: Path, prefix: str,
                     excluded_roots: Iterable[str] = ()) -> list[tuple[str, str]]:
    """Inventory one directory without following a directory link/reparse point."""
    excluded = set(excluded_roots)
    entries: list[tuple[str, str]] = []

    def walk(directory: Path, relative: PurePosixPath) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as e:
            raise AssentError(f"Unable to snapshot review surface {directory}: {e}") from e
        for child in children:
            child_rel = relative / child.name
            rel_text = child_rel.as_posix()
            if not relative.parts and child.name in excluded:
                continue
            key = f"{prefix}:{rel_text}"
            try:
                info = child.stat(follow_symlinks=False)
                attributes = getattr(info, "st_file_attributes", 0)
                reparse = bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
                if child.is_symlink() or reparse:
                    try:
                        target = os.readlink(child.path)
                    except OSError:
                        target = ""
                    identity = hashlib.sha256(
                        (f"link\0{info.st_mode}\0{attributes}\0{target}").encode(
                            "utf-8", errors="surrogatepass")).hexdigest()
                    entries.append((key, f"link:{identity}"))
                elif child.is_dir(follow_symlinks=False):
                    entries.append((key, "directory"))
                    walk(Path(child.path), child_rel)
                elif child.is_file(follow_symlinks=False):
                    entries.append((key, f"file:{info.st_size}:{_file_sha256(Path(child.path))}"))
                else:
                    entries.append((key, f"other:{info.st_mode}"))
            except OSError as e:
                raise AssentError(
                    f"Unable to snapshot review surface entry {child.path}: {e}") from e

    walk(root, PurePosixPath())
    return entries


def snapshot_project_surface(source_root: Path,
                             assent_dir: Path,
                             primary_root: Path | None = None,
                             *,
                             tasks_dir: Path | None = None,
                             stable_management_files: Iterable[Path] = (),
                             ) -> ProjectSurfaceSnapshot:
    """Snapshot source plus the management surfaces protected during review.

    A production caller supplies ``tasks_dir`` and the stable root management
    inputs it consumed.  That deliberately excludes the active terminal log and
    every unrelated work folder, whose scheduler-owned files may advance while
    another folder is being reviewed.  Omitting ``tasks_dir`` retains the
    general whole-directory form used by lower-level callers.
    """
    source_root = Path(source_root)
    assent_dir = Path(assent_dir)
    if not source_root.is_dir():
        raise AssentError(f"Auto-fix review source is not a directory: {source_root}")
    if not assent_dir.is_dir():
        raise AssentError(
            f"Auto-fix review management plane is not a directory: {assent_dir}")
    entries = _surface_entries(source_root, "source", {".git", ".assent"})
    if primary_root is not None:
        primary_root = Path(primary_root)
        if not primary_root.is_dir():
            raise AssentError(
                f"Auto-fix review primary tree is not a directory: {primary_root}")
        if primary_root.resolve() != source_root.resolve():
            entries.extend(_surface_entries(
                primary_root, "primary", {".git", ".assent"}))
    if tasks_dir is None:
        entries.extend(_surface_entries(assent_dir, "management"))
    else:
        tasks_dir = Path(tasks_dir)
        try:
            tasks_rel = tasks_dir.absolute().relative_to(
                assent_dir.absolute()).as_posix()
        except ValueError as e:
            raise AssentError(
                f"Auto-fix review task folder is outside the management plane: "
                f"{tasks_dir}") from e
        if not tasks_dir.is_dir():
            raise AssentError(
                f"Auto-fix review task folder is not a directory: {tasks_dir}")
        entries.extend(_surface_entries(
            tasks_dir, f"management:{tasks_rel}", {"_assent.log"}))

        seen: set[Path] = set()
        for path in stable_management_files:
            path = Path(path)
            try:
                relative = path.absolute().relative_to(assent_dir.absolute())
            except ValueError as e:
                raise AssentError(
                    f"Auto-fix review management input is outside {assent_dir}: "
                    f"{path}") from e
            if path in seen:
                continue
            seen.add(path)
            key = f"management:{relative.as_posix()}"
            try:
                info = path.stat(follow_symlinks=False)
            except FileNotFoundError:
                entries.append((key, "missing"))
                continue
            except OSError as e:
                raise AssentError(
                    f"Unable to snapshot auto-fix management input {path}: {e}") from e
            attributes = getattr(info, "st_file_attributes", 0)
            reparse = bool(
                attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            if path.is_symlink() or reparse:
                try:
                    target = os.readlink(path)
                except OSError:
                    target = ""
                identity = hashlib.sha256(
                    (f"link\0{info.st_mode}\0{attributes}\0{target}").encode(
                        "utf-8", errors="surrogatepass")).hexdigest()
                entries.append((key, f"link:{identity}"))
            elif stat.S_ISREG(info.st_mode):
                entries.append((key, f"file:{info.st_size}:{_file_sha256(path)}"))
            elif stat.S_ISDIR(info.st_mode):
                entries.append((key, "directory"))
                entries.extend(_surface_entries(path, key))
            else:
                entries.append((key, f"other:{info.st_mode}"))
    return ProjectSurfaceSnapshot(tuple(sorted(entries)))


def sha256_files(paths: Iterable[Path]) -> str:
    """Hash an ordered set of named files, including names and exact bytes."""
    digest = hashlib.sha256()
    for path in sorted((Path(path) for path in paths), key=lambda item: item.name):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise AssentError(f"Unable to hash auto-fix input {path}: {e}") from e
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _require_exact_keys(data: dict, expected: set[str], label: str) -> None:
    unknown = sorted(set(data) - expected)
    missing = sorted(expected - set(data))
    if unknown:
        raise AssentError(f"{label} has unknown keys: {', '.join(unknown)}")
    if missing:
        raise AssentError(f"{label} is missing keys: {', '.join(missing)}")


def _require_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AssentError(f"{label} must be a string")
    if not value.strip():
        raise AssentError(f"{label} must not be blank")
    if _CONTROL_RE.search(value):
        raise AssentError(f"{label} must not contain control characters")
    if len(value) > limit:
        raise AssentError(f"{label} exceeds the size limit")
    return value


def _require_multiline_text(value: object, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise AssentError(f"{label} must be a string")
    if not value.strip():
        raise AssentError(f"{label} must not be blank")
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
        raise AssentError(f"{label} must not contain unsafe control characters")
    if len(value) > limit:
        raise AssentError(f"{label} exceeds the size limit")
    return value


def normalize_finding_path(value: object) -> str:
    """Return one unambiguous POSIX project-relative path or refuse it."""
    path = _require_text(value, "Review finding path", MAX_PATH_LENGTH)
    path = path.replace("\\", "/")
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        raise AssentError("Review finding path must be project-relative")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AssentError("Review finding path must be normalized and must not traverse")
    normalized = PurePosixPath(*parts).as_posix()
    if normalized in {"", "."}:
        raise AssentError("Review finding path must name a project-relative entry")
    return normalized


def scheduler_finding_path(scope_path: object) -> str:
    """Canonicalize one trusted scope prefix for a scheduler-owned finding.

    Reviewer paths remain strictly normalized.  Scheduler findings sometimes
    stand for a whole directory scope, whose canonical project-relative entry
    is the prefix without its declaration-only trailing separator.
    """
    path = _require_text(scope_path, "Scheduler finding scope", MAX_PATH_LENGTH)
    path = path.replace("\\", "/").rstrip("/")
    return normalize_finding_path(path)


def _validate_scope_addition(value: object, label: str) -> ScopeAddition | None:
    if value is None:
        return None
    if not isinstance(value, ScopeAddition):
        raise AssentError(f"{label} must be null or a scope addition")
    if not isinstance(value.path_state, str) or value.path_state not in SCOPE_PATH_STATES:
        raise AssentError(
            f"{label} path_state must be existing_file or new_file")
    return ScopeAddition(normalize_finding_path(value.path), value.path_state)


def _validate_finding(finding: ReviewFinding, label: str) -> ReviewFinding:
    if not isinstance(finding, ReviewFinding):
        raise AssentError(f"{label} must be a review finding")
    task_id = finding.task_id
    if task_id is not None and (not isinstance(task_id, str)
                                or not _TASK_ID_RE.fullmatch(task_id)):
        raise AssentError(f"{label} task_id must be null or a tNNN task id")
    if not isinstance(finding.kind, str) or finding.kind not in REVIEW_FINDING_KINDS:
        raise AssentError(f"{label} kind is not supported")
    scope_addition = _validate_scope_addition(
        finding.scope_addition, f"{label} scope_addition")
    if (finding.kind == "scope_amendment") != (scope_addition is not None):
        raise AssentError(
            f"{label} kind scope_amendment requires exactly one scope_addition")
    path = normalize_finding_path(finding.path)
    if scope_addition is not None and scope_addition.path != path:
        raise AssentError(f"{label} scope_addition must name the finding path")
    if (not isinstance(finding.transition, str)
            or finding.transition not in REVIEW_TRANSITION_KINDS):
        raise AssentError(f"{label} transition is not supported")
    prior = finding.prior_fingerprint
    transition_evidence = finding.transition_evidence
    if finding.transition == "initial":
        if prior is not None or transition_evidence is not None:
            raise AssentError(
                f"{label} initial transition must not cite prior or transition evidence")
    elif finding.transition == "still_present":
        _require_digest(prior, f"{label} prior_fingerprint")
        transition_evidence = _require_text(
            transition_evidence, f"{label} transition_evidence",
            MAX_EVIDENCE_LENGTH)
    else:
        if prior is not None:
            raise AssentError(
                f"{label} {finding.transition} must not cite a prior fingerprint")
        transition_evidence = _require_text(
            transition_evidence, f"{label} transition_evidence",
            MAX_EVIDENCE_LENGTH)
    return ReviewFinding(
        task_id=task_id,
        path=path,
        summary=_require_text(finding.summary, f"{label} summary",
                              MAX_SUMMARY_LENGTH),
        evidence=_require_text(finding.evidence, f"{label} evidence",
                               MAX_EVIDENCE_LENGTH),
        kind=finding.kind,
        recommendation=_require_text(
            finding.recommendation, f"{label} recommendation",
            MAX_RECOMMENDATION_LENGTH),
        scope_addition=scope_addition,
        transition=finding.transition,
        prior_fingerprint=prior,
        transition_evidence=transition_evidence,
    )


def finding_fingerprint(finding: ReviewFinding) -> str:
    """Compute the stable identity; reviewers never supply this value."""
    finding = _validate_finding(finding, "Review finding")
    scope_addition = None
    if finding.scope_addition is not None:
        scope_addition = {
            "path": finding.scope_addition.path,
            "path_state": finding.scope_addition.path_state,
        }
    canonical = json.dumps(
        {"kind": finding.kind, "task_id": finding.task_id,
         "path": finding.path, "summary": finding.summary,
         "evidence": finding.evidence,
         "recommendation": finding.recommendation,
         "scope_addition": scope_addition},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_review_record(record: ReviewRecord) -> ReviewRecord:
    if not isinstance(record, ReviewRecord):
        raise AssentError("Auto-fix review verdict must be a review record")
    if not isinstance(record.verdict, str) or record.verdict not in REVIEW_VERDICTS:
        raise AssentError("Auto-fix review verdict must be PASS or FAIL")
    if not isinstance(record.findings, tuple):
        raise AssentError("Auto-fix review findings must be a finite list")
    if len(record.findings) > MAX_FINDINGS:
        raise AssentError("Auto-fix review has too many findings")
    findings = tuple(
        _validate_finding(finding, f"Review findings[{index}]")
        for index, finding in enumerate(record.findings)
    )
    if record.verdict == "PASS" and findings:
        raise AssentError("A PASS auto-fix review must have no blocking findings")
    if record.verdict == "FAIL" and not findings:
        raise AssentError("A FAIL auto-fix review must have a blocking finding")
    fingerprints = [finding_fingerprint(finding) for finding in findings]
    if len(fingerprints) != len(set(fingerprints)):
        raise AssentError("Auto-fix review contains a duplicate finding")
    return ReviewRecord(record.verdict, findings)


def review_record_json(record: ReviewRecord) -> str:
    """Serialize a valid terminal record deterministically on one JSON line."""
    record = _validate_review_record(record)
    data = {
        "type": REVIEW_RECORD_TYPE,
        "verdict": record.verdict,
        "findings": [
            {"kind": finding.kind, "task_id": finding.task_id,
             "path": finding.path, "summary": finding.summary,
             "evidence": finding.evidence,
             "recommendation": finding.recommendation,
             "scope_addition": (
                 {"path": finding.scope_addition.path,
                  "path_state": finding.scope_addition.path_state}
                 if finding.scope_addition is not None else None),
             "transition": finding.transition,
             "prior_fingerprint": finding.prior_fingerprint,
             "transition_evidence": finding.transition_evidence}
            for finding in record.findings
        ],
    }
    text = json.dumps(data, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
    if len(text.encode("utf-8")) > MAX_REVIEW_RECORD_BYTES:
        raise AssentError("Auto-fix review terminal record exceeds the size limit")
    return text


def _record_from_data(data: object) -> ReviewRecord:
    if not isinstance(data, dict):
        raise AssentError("Auto-fix review terminal record must be a JSON object")
    _require_exact_keys(data, _REVIEW_KEYS, "Auto-fix review terminal record")
    if data["type"] != REVIEW_RECORD_TYPE:
        raise AssentError(
            f"Auto-fix review terminal record type must be {REVIEW_RECORD_TYPE!r}")
    if not isinstance(data["findings"], list):
        raise AssentError("Auto-fix review findings must be a finite JSON list")
    findings: list[ReviewFinding] = []
    for index, raw in enumerate(data["findings"]):
        if not isinstance(raw, dict):
            raise AssentError(f"Review findings[{index}] must be a JSON object")
        _require_exact_keys(raw, _FINDING_KEYS, f"Review findings[{index}]")
        raw_scope = raw["scope_addition"]
        scope_addition = None
        if raw_scope is not None:
            if not isinstance(raw_scope, dict):
                raise AssentError(
                    f"Review findings[{index}] scope_addition must be null or an object")
            _require_exact_keys(
                raw_scope, _SCOPE_ADDITION_KEYS,
                f"Review findings[{index}] scope_addition")
            scope_addition = ScopeAddition(**raw_scope)
        findings.append(ReviewFinding(
            task_id=raw["task_id"], path=raw["path"],
            summary=raw["summary"], evidence=raw["evidence"],
            kind=raw["kind"], recommendation=raw["recommendation"],
            scope_addition=scope_addition, transition=raw["transition"],
            prior_fingerprint=raw["prior_fingerprint"],
            transition_evidence=raw["transition_evidence"],
        ))
    return _validate_review_record(ReviewRecord(data["verdict"], tuple(findings)))


def parse_review_output(output: str | bytes) -> ReviewRecord:
    """Extract exactly one final review record from adapter output, fail closed."""
    if isinstance(output, bytes):
        try:
            raw = output.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AssentError("Auto-fix review output is not valid UTF-8") from e
    elif isinstance(output, str):
        raw = output
    else:
        raise AssentError("Auto-fix review output must be text or UTF-8 bytes")
    if len(raw.encode("utf-8")) > MAX_REVIEW_OUTPUT_BYTES:
        raise AssentError("Auto-fix review output exceeds the size limit")

    nonempty = [(index, line.strip()) for index, line in enumerate(raw.splitlines())
                if line.strip()]
    candidates: list[tuple[int, str, object]] = []
    malformed_marker = False
    for index, line in nonempty:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if REVIEW_RECORD_TYPE in line:
                malformed_marker = True
            continue
        if isinstance(value, dict) and value.get("type") == REVIEW_RECORD_TYPE:
            candidates.append((index, line, value))
    if malformed_marker:
        raise AssentError("Auto-fix review output contains a malformed terminal record")
    if not candidates:
        raise AssentError("Auto-fix review output has no terminal review record")
    if len(candidates) != 1:
        raise AssentError("Auto-fix review output has duplicate terminal review records")
    index, line, data = candidates[0]
    if nonempty[-1][0] != index:
        raise AssentError("Auto-fix review output has trailing non-empty output")
    if len(line.encode("utf-8")) > MAX_REVIEW_RECORD_BYTES:
        raise AssentError("Auto-fix review terminal record exceeds the size limit")
    return _record_from_data(data)


parse_review_record = parse_review_output
serialize_review_record = review_record_json


def persisted_finding(finding: ReviewFinding) -> PersistedFinding:
    finding = _validate_finding(finding, "Review finding")
    return PersistedFinding(
        fingerprint=finding_fingerprint(finding), kind=finding.kind,
        task_id=finding.task_id,
        path=finding.path, summary=finding.summary, evidence=finding.evidence,
        recommendation=finding.recommendation,
        scope_addition_path=(finding.scope_addition.path
                             if finding.scope_addition else None),
        scope_addition_path_state=(finding.scope_addition.path_state
                                   if finding.scope_addition else None),
    )


def _path_is_in_scope(path: str, scope: Iterable[str]) -> bool:
    """Apply the execution engine's literal project-relative prefix rule."""
    normalized = path.replace("\\", "/")
    for item in scope:
        prefix = item.replace("\\", "/").rstrip("/")
        if prefix and (normalized == prefix
                       or normalized.startswith(prefix + "/")):
            return True
    return False


def validate_review_findings(record: ReviewRecord, plan: "Plan") -> ReviewRecord:
    """Resolve every finding to exactly one existing task and its declared scope.

    A reviewer may omit ``task_id`` only when the path itself has one unambiguous
    owner.  A normalized, exact-path scope amendment tied to an existing task
    is the sole exception to the ordinary declared-scope ownership rule.
    """
    record = _validate_review_record(record)
    tasks = {task.id: task for task in plan.tasks}
    resolved: list[ReviewFinding] = []
    for finding in record.findings:
        if finding.task_id is not None:
            task = tasks.get(finding.task_id)
            if task is None:
                raise AssentError(
                    f"Auto-fix review finding names unknown task id: {finding.task_id}")
            if (not _path_is_in_scope(finding.path, task.scope)
                    and finding.scope_addition is None):
                raise AssentError(
                    f"Auto-fix review finding path {finding.path!r} is outside "
                    f"{finding.task_id}'s declared scope")
            resolved.append(finding)
            continue

        if finding.scope_addition is not None:
            raise AssentError(
                "Auto-fix scope amendment must name the existing task to amend")
        owners = [task for task in plan.tasks
                  if _path_is_in_scope(finding.path, task.scope)]
        if not owners:
            raise AssentError(
                f"Auto-fix review finding path {finding.path!r} is outside every "
                "existing task's declared scope")
        if len(owners) != 1:
            names = ", ".join(task.id for task in owners)
            raise AssentError(
                f"Auto-fix review finding path {finding.path!r} has ambiguous task "
                f"ownership: {names}")
        resolved.append(replace(finding, task_id=owners[0].id))
    return ReviewRecord(record.verdict, tuple(resolved))


def validate_scope_additions(
        root: Path, plan: "Plan",
        additions: Iterable[ApprovedScopeAddition],
        ) -> tuple[ApprovedScopeAddition, ...]:
    """Validate a complete reviewer scope decision without following links.

    The caller must pass the pre-amendment plan. All decisions are checked
    before this function returns, so its result is safe to hand to the atomic
    task-contract writer without permitting a partially validated batch.
    """
    root = Path(root).absolute()
    try:
        root_info = os.lstat(root)
    except OSError as e:
        raise AssentError(f"Unable to inspect scope-amendment root {root}: {e}") from e
    if (pathops.is_link_stat(root_info) or pathops.is_reparse_point(root_info)
            or not stat.S_ISDIR(root_info.st_mode)):
        raise AssentError(
            "Scope-amendment root must be an ordinary repository directory")

    items = tuple(additions)
    tasks = {task.id: task for task in plan.tasks}
    seen_paths: set[str] = set()
    for index, item in enumerate(items):
        label = f"Approved scope addition[{index}]"
        if not isinstance(item, ApprovedScopeAddition):
            raise AssentError(f"{label} is invalid")
        _require_digest(item.fingerprint, f"{label} fingerprint")
        task = tasks.get(item.task_id)
        if task is None:
            raise AssentError(
                f"{label} names unknown task id: {item.task_id}")
        normalized = normalize_finding_path(item.path)
        if normalized != item.path:
            raise AssentError(f"{label} path must already be normalized")
        if item.path_state not in SCOPE_PATH_STATES:
            raise AssentError(f"{label} path_state is invalid")
        portable_identity = normalized.casefold()
        if portable_identity in seen_paths:
            raise AssentError(
                "Auto-fix scope decision contains a duplicate or ambiguous path")
        seen_paths.add(portable_identity)

        parts = PurePosixPath(normalized).parts
        protected = {".git", ".assent", "_archive"}
        if any(part.casefold() in protected for part in parts):
            raise AssentError(
                f"Auto-fix scope path targets a protected control surface: "
                f"{normalized}")
        # A protected basename is refused wherever it sits: a nested AGENTS.md
        # or .gitignore governs its own subtree exactly as the root one does.
        leaf = parts[-1].casefold()
        if (re.fullmatch(r"t\d{3}_.+\.(?:e|r)\.toml", leaf)
                or leaf in _PROTECTED_SCOPE_BASENAMES):
            raise AssentError(
                f"Auto-fix scope path targets a protected control surface: "
                f"{normalized}")
        if any(character in normalized for character in "*?[]{}"):
            raise AssentError(
                f"Auto-fix scope path must be exact, not a glob: {normalized}")
        folded = normalized.casefold()
        if _path_is_in_scope(normalized, task.scope) or _path_is_in_scope(
                folded, (item.casefold() for item in task.scope)):
            raise AssentError(
                f"Auto-fix scope path is already covered by {task.id}: {normalized}")
        other_owners = [
            candidate.id for candidate in plan.tasks
            if candidate.id != task.id
            and (_path_is_in_scope(normalized, candidate.scope)
                 or _path_is_in_scope(
                     folded, (item.casefold() for item in candidate.scope)))
        ]
        if other_owners:
            raise AssentError(
                f"Auto-fix scope path has ambiguous ownership with "
                f"{', '.join(other_owners)}: {normalized}")

        current = root
        for component_index, component in enumerate(parts):
            current = current / component
            final = component_index == len(parts) - 1
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                if not final:
                    raise AssentError(
                        f"Auto-fix scope path has a missing parent: {normalized}")
                if item.path_state != "new_file":
                    raise AssentError(
                        f"Auto-fix scope path_state existing_file does not match "
                        f"the absent target: {normalized}")
                break
            except OSError as e:
                raise AssentError(
                    f"Unable to inspect auto-fix scope path {normalized}: {e}") from e
            if pathops.is_link_stat(info) or pathops.is_reparse_point(info):
                raise AssentError(
                    f"Auto-fix scope path traverses a link or reparse point: "
                    f"{normalized}")
            if final:
                if item.path_state == "new_file":
                    raise AssentError(
                        f"Auto-fix scope path_state new_file does not match the "
                        f"existing target: {normalized}")
                if not stat.S_ISREG(info.st_mode):
                    raise AssentError(
                        f"Auto-fix existing_file target is not an ordinary file: "
                        f"{normalized}")
            elif not stat.S_ISDIR(info.st_mode):
                raise AssentError(
                    f"Auto-fix scope path parent is not an ordinary directory: "
                    f"{normalized}")
    return items


def validate_review_transitions(
        record: ReviewRecord, *, review_stage: str,
        previous: AutoFixState | None = None,
        repair_changed_paths: Iterable[str] | None = None) -> ReviewRecord:
    """Validate scheduler-issued identity continuity for one review stage."""
    record = _validate_review_record(record)
    if review_stage not in REVIEW_STAGES:
        raise AssentError("Auto-fix review stage must be initial or recheck")
    if review_stage == "initial":
        if previous is not None:
            raise AssentError("An initial auto-fix review must not cite prior state")
        if any(item.transition != "initial" for item in record.findings):
            raise AssentError("An initial review accepts only initial findings")
        return record
    if previous is None:
        raise AssentError("An auto-fix recheck requires prior durable state")
    previous = _validate_state(previous)
    current = set(previous.current_finding_fingerprints)
    ledger = {item.fingerprint: item for item in previous.findings}
    current_locations = {
        (ledger[fingerprint].task_id, ledger[fingerprint].path,
         ledger[fingerprint].kind)
        for fingerprint in current
    }
    changed_paths = None
    if repair_changed_paths is not None:
        changed_paths = tuple(
            normalize_finding_path(path) for path in repair_changed_paths)
    for index, item in enumerate(record.findings):
        label = f"Review findings[{index}]"
        fingerprint = finding_fingerprint(item)
        if item.transition == "initial":
            raise AssentError("An auto-fix recheck cannot contain an initial finding")
        if (item.kind == "eligible_technical_debt"
                and item.transition != "still_present"):
            raise AssentError(
                f"{label} cannot introduce eligible technical debt during recheck")
        if item.transition == "still_present":
            if item.prior_fingerprint not in current:
                raise AssentError(
                    f"{label} still_present must cite a current scheduler fingerprint")
            if fingerprint != item.prior_fingerprint:
                raise AssentError(
                    f"{label} still_present changed immutable substantive fields")
        elif fingerprint in ledger:
            raise AssentError(
                f"{label} {item.transition} must identify a genuinely new blocker")
        elif (item.task_id, item.path, item.kind) in current_locations:
            raise AssentError(
                f"{label} is an unsupported wording variant of a current finding")
        elif item.transition == "repair_regression" and changed_paths is not None:
            path = item.path.rstrip("/")
            if not any(
                    changed == path or changed.startswith(path + "/")
                    or path.startswith(changed.rstrip("/") + "/")
                    for changed in changed_paths):
                raise AssentError(
                    f"{label} repair_regression is not tied to the repair delta")
        elif (item.transition == "newly_exposed"
              and repair_changed_paths is not None):
            origin = (item.transition_evidence or "").lower()
            task_token = (item.task_id or "").lower()
            if (not task_token or task_token not in origin
                    or not any(word in origin for word in (
                        "requirement", "acceptance", "behavior", "goal"))):
                raise AssentError(
                    f"{label} newly_exposed does not identify an existing requirement")
    return record


def current_review_record(state: AutoFixState) -> ReviewRecord:
    """Reconstruct the current validated verdict from durable ledger entries."""
    state = _validate_state(state)
    by_fingerprint = {item.fingerprint: item for item in state.findings}
    findings = tuple(
        by_fingerprint[fingerprint].finding
        for fingerprint in state.current_finding_fingerprints)
    return _validate_review_record(ReviewRecord(state.verdict, findings))


def consume_fixer_profile(state: AutoFixState,
                          profile: FixerProfile) -> AutoFixState:
    """Persistently consume one unique repair profile before its session starts."""
    state = _validate_state(state)
    candidate = replace_fixer_profiles(
        state, state.consumed_fixer_profiles + (profile,))
    return _validate_state(candidate)


def replace_fixer_profiles(
        state: AutoFixState,
        profiles: tuple[FixerProfile, ...]) -> AutoFixState:
    """Return a state copy with a caller-supplied ordered profile history."""
    return replace(state, consumed_fixer_profiles=profiles)


def with_repair_phase(state: AutoFixState, phase: str) -> AutoFixState:
    """Durably distinguish an active repair from its pending re-review."""
    state = _validate_state(state)
    if phase not in {"REPAIRING", "AWAITING_REVIEW"}:
        raise AssentError("Auto-fix repair phase must be REPAIRING or AWAITING_REVIEW")
    return _validate_state(replace(state, phase=phase))


def with_repair_briefs(
        state: AutoFixState,
        briefs: tuple[RepairBrief, ...]) -> AutoFixState:
    """Persist the exact current reviewer-to-worker handoff before mutation."""
    state = _validate_state(state)
    return _validate_state(replace(state, repair_briefs=briefs))


def with_worker_dispositions(
        state: AutoFixState,
        dispositions: tuple[WorkerDisposition, ...]) -> AutoFixState:
    """Persist validated worker acknowledgement evidence for the next recheck."""
    state = _validate_state(state)
    return _validate_state(replace(state, worker_dispositions=dispositions))


def with_repair_round_assignments(
        state: AutoFixState,
        assignments: tuple[RepairRoundAssignment, ...]) -> AutoFixState:
    """Persist the complete current round before or after an attempt starts."""
    state = _validate_state(state)
    return _validate_state(replace(
        state, repair_round_assignments=assignments))


def with_plan_digest_transition(
        state: AutoFixState, before_sha256: str,
        after_sha256: str) -> AutoFixState:
    """Record one scheduler-owned plan amendment exactly once."""
    state = _validate_state(state)
    _require_digest(before_sha256, "Plan transition before_sha256")
    _require_digest(after_sha256, "Plan transition after_sha256")
    transition = PlanDigestTransition(before_sha256, after_sha256)
    if (state.task_plan_sha256 == after_sha256
            and transition in state.plan_digest_transitions):
        return state
    if state.task_plan_sha256 != before_sha256:
        raise AssentError(
            "Auto-fix state no longer matches the pre-amendment task plan")
    if transition in state.plan_digest_transitions:
        raise AssentError(
            "Auto-fix plan transition is present without its resulting plan digest")
    return _validate_state(replace(
        state, task_plan_sha256=after_sha256,
        plan_digest_transitions=state.plan_digest_transitions + (transition,)))


def with_scope_amendments(
        state: AutoFixState,
        amendments: tuple[ScopeAmendment, ...]) -> AutoFixState:
    """Persist precomputed scheduler amendments before any task file changes."""
    state = _validate_state(state)
    return _validate_state(replace(state, scope_amendments=amendments))


def next_unused_fixer_profile(
        state: AutoFixState,
        profiles: Iterable[FixerProfile]) -> FixerProfile | None:
    """Return the first unique profile not already consumed by this folder."""
    state = _validate_state(state)
    used = {(item.adapter, item.model, item.effort)
            for item in state.consumed_fixer_profiles}
    seen = set(used)
    for profile in profiles:
        identity = (profile.adapter, profile.model, profile.effort)
        if identity in seen:
            continue
        seen.add(identity)
        return profile
    return None


def auto_fix_state_path(config_or_folder: Config | str | Path) -> Path:
    """Return the derived state path for a Config or explicit folder directory."""
    if isinstance(config_or_folder, Config):
        folder = config_or_folder.tasks_dir
    else:
        folder = Path(config_or_folder)
    return Path(folder) / AUTO_FIX_STATE_NAME


state_path = auto_fix_state_path


def _require_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not DIGEST_RE.fullmatch(value):
        raise AssentError(f"{label} must be a 64-character lowercase hexadecimal digest")
    return value


def parse_repair_dispositions(
        detail: object, *, task_id: str, task_status: str,
        expected_fingerprints: Iterable[str]) -> tuple[WorkerDisposition, ...]:
    """Parse the exact per-finding acknowledgement contract from journal detail.

    The acknowledgement is deliberately only audit evidence.  Callers still run
    the ordinary structural, scope, focused, and independent-review gates.
    """
    if not isinstance(task_id, str) or not _TASK_ID_RE.fullmatch(task_id):
        raise AssentError("Repair disposition task_id must be a tNNN task id")
    if task_status not in {"DONE", "BLOCKED"}:
        raise AssentError(
            "Repair dispositions require a DONE or BLOCKED task closeout")
    if not isinstance(detail, str):
        raise AssentError("Repair closeout journal detail must be a string")
    expected = tuple(expected_fingerprints)
    if not expected:
        raise AssentError("Repair disposition gate requires current findings")
    if len(set(expected)) != len(expected):
        raise AssentError("Repair disposition gate received duplicate findings")
    for fingerprint in expected:
        _require_digest(fingerprint, "Expected repair finding fingerprint")

    parsed: dict[str, WorkerDisposition] = {}
    token = REPAIR_DISPOSITION_PREFIX.rstrip()
    for line_number, line in enumerate(detail.splitlines(), 1):
        if token not in line:
            continue
        if not line.startswith(REPAIR_DISPOSITION_PREFIX):
            raise AssentError(
                f"Malformed repair disposition line {line_number}")
        payload = line[len(REPAIR_DISPOSITION_PREFIX):]
        try:
            raw = json.loads(payload)
        except json.JSONDecodeError as e:
            raise AssentError(
                f"Repair disposition line {line_number} is not valid JSON: {e}") from e
        if not isinstance(raw, dict):
            raise AssentError(
                f"Repair disposition line {line_number} must contain a JSON object")
        _require_exact_keys(
            raw, {"fingerprint", "disposition", "detail"},
            f"Repair disposition line {line_number}")
        fingerprint = _require_digest(
            raw["fingerprint"],
            f"Repair disposition line {line_number} fingerprint")
        disposition = _require_text(
            raw["disposition"],
            f"Repair disposition line {line_number} disposition", 100)
        concrete_detail = _require_text(
            raw["detail"],
            f"Repair disposition line {line_number} detail",
            MAX_EVIDENCE_LENGTH)
        if disposition not in REPAIR_DISPOSITIONS:
            raise AssentError(
                f"Repair disposition line {line_number} has an unknown disposition")
        if fingerprint not in expected:
            raise AssentError(
                f"Repair disposition line {line_number} names an unknown fingerprint")
        if fingerprint in parsed:
            raise AssentError(
                f"Repair disposition line {line_number} duplicates a fingerprint")
        if disposition == "still_blocked" and task_status != "BLOCKED":
            raise AssentError(
                "A still_blocked repair disposition requires BLOCKED closeout")
        parsed[fingerprint] = WorkerDisposition(
            task_id, fingerprint, disposition, concrete_detail)

    missing = [fingerprint for fingerprint in expected
               if fingerprint not in parsed]
    if missing:
        raise AssentError(
            "Repair closeout is missing disposition fingerprints: "
            + ", ".join(missing))
    return tuple(parsed[fingerprint] for fingerprint in expected)


def _require_tree(value: object, label: str) -> str:
    if not isinstance(value, str) or not OID_RE.fullmatch(value):
        raise AssentError(
            f"{label} must be a 40- or 64-character lowercase hexadecimal tree id")
    return value


def _validate_state(state: AutoFixState) -> AutoFixState:
    if not isinstance(state, AutoFixState):
        raise AssentError("Auto-fix state must be an AutoFixState record")
    if type(state.version) is not int or state.version != AUTO_FIX_STATE_VERSION:
        raise AssentError(f"Auto-fix state version must be {AUTO_FIX_STATE_VERSION}")
    _require_tree(state.source_tree, "Auto-fix state source_tree")
    _require_digest(state.task_plan_sha256, "Auto-fix state task_plan_sha256")
    _require_digest(state.review_prompt_sha256,
                    "Auto-fix state review_prompt_sha256")
    for name in ("reviewer_adapter", "reviewer_model", "reviewer_effort"):
        _require_text(getattr(state, name), f"Auto-fix state {name}", 1024)
    if not isinstance(state.phase, str) or state.phase not in AUTO_FIX_PHASES:
        raise AssentError("Auto-fix state phase is invalid")
    if not isinstance(state.verdict, str) or state.verdict not in REVIEW_VERDICTS:
        raise AssentError("Auto-fix state verdict must be PASS or FAIL")
    if state.review_context not in REVIEW_CONTEXTS:
        raise AssentError("Auto-fix state review_context is invalid")
    if state.review_stage not in REVIEW_STAGES:
        raise AssentError("Auto-fix state review_stage is invalid")
    if state.review_context == "completed_folder":
        if state.failure_trigger is not None:
            raise AssentError(
                "A completed-folder review must not have a failure trigger")
    elif state.failure_trigger not in FAILURE_TRIGGERS:
        raise AssentError(
            "A blocked adjudication requires a worker_blocked or focused_gate_failure trigger")
    if state.verdict == "PASS" and state.phase != "COMPLETE":
        raise AssentError("A PASS auto-fix state must be COMPLETE")
    if state.verdict == "FAIL" and state.phase == "COMPLETE":
        raise AssentError("A FAIL auto-fix state must not be COMPLETE")
    for name in ("current_finding_fingerprints", "findings",
                 "observed_states", "reviewer_recommendations",
                 "approved_scope_additions", "scope_amendments",
                 "worker_dispositions", "repair_briefs",
                 "repair_round_assignments", "plan_digest_transitions",
                 "review_transitions", "consumed_fixer_profiles"):
        if not isinstance(getattr(state, name), tuple):
            raise AssentError(f"Auto-fix state {name} must be an ordered list")

    ledger: dict[str, PersistedFinding] = {}
    for index, item in enumerate(state.findings):
        if not isinstance(item, PersistedFinding):
            raise AssentError(f"Auto-fix state findings[{index}] is invalid")
        finding = _validate_finding(item.finding, f"State findings[{index}]")
        expected = finding_fingerprint(finding)
        _require_digest(item.fingerprint,
                        f"Auto-fix state findings[{index}] fingerprint")
        if item.fingerprint != expected:
            raise AssentError(
                f"Auto-fix state findings[{index}] fingerprint does not match its finding")
        if item.fingerprint in ledger:
            raise AssentError("Auto-fix state finding ledger has a duplicate fingerprint")
        ledger[item.fingerprint] = item
        scope_values = (item.scope_addition_path, item.scope_addition_path_state)
        if (scope_values[0] is None) != (scope_values[1] is None):
            raise AssentError(
                f"Auto-fix state findings[{index}] has an incomplete scope addition")

    current = state.current_finding_fingerprints
    current_seen: set[str] = set()
    for fingerprint in current:
        _require_digest(fingerprint, "Auto-fix state current finding fingerprint")
        if fingerprint in current_seen:
            raise AssentError("Auto-fix state current findings contain a duplicate")
        current_seen.add(fingerprint)
        if fingerprint not in ledger:
            raise AssentError(
                "Auto-fix state current finding is absent from the finding ledger")
    if state.verdict == "PASS" and current:
        raise AssentError("A PASS auto-fix state must have no current findings")
    if state.verdict == "FAIL" and not current:
        raise AssentError("A FAIL auto-fix state must have current findings")
    debt_fingerprints = {
        fingerprint for fingerprint in current
        if ledger[fingerprint].kind == "eligible_technical_debt"}
    if debt_fingerprints:
        if state.review_context != "completed_folder":
            raise AssentError(
                "Eligible technical debt is limited to completed-folder review")

    expected_recommendations = tuple(
        ReviewerRecommendation(item, ledger[item].recommendation)
        for item in current)
    if state.reviewer_recommendations != expected_recommendations:
        raise AssentError(
            "Auto-fix state reviewer_recommendations must match current findings")

    approved_seen: set[tuple[str, str]] = set()
    for index, item in enumerate(state.approved_scope_additions):
        if not isinstance(item, ApprovedScopeAddition):
            raise AssentError(
                f"Auto-fix state approved_scope_additions[{index}] is invalid")
        _require_digest(item.fingerprint, "Approved scope-addition fingerprint")
        persisted = ledger.get(item.fingerprint)
        if persisted is None or persisted.scope_addition_path is None:
            raise AssentError("Approved scope addition is absent from the finding ledger")
        if not isinstance(item.task_id, str) or not _TASK_ID_RE.fullmatch(item.task_id):
            raise AssentError("Approved scope addition task_id must be a tNNN task id")
        normalized = normalize_finding_path(item.path)
        if item.path_state not in SCOPE_PATH_STATES:
            raise AssentError("Approved scope addition path_state is invalid")
        if (item.task_id != persisted.task_id or normalized != persisted.scope_addition_path
                or item.path_state != persisted.scope_addition_path_state):
            raise AssentError("Approved scope addition does not match its finding")
        identity = (item.task_id, normalized)
        if identity in approved_seen:
            raise AssentError("Auto-fix state has a duplicate approved scope addition")
        approved_seen.add(identity)

    approved_by_fingerprint = {
        item.fingerprint: item for item in state.approved_scope_additions}
    amended_fingerprints: set[str] = set()
    for index, item in enumerate(state.scope_amendments):
        label = f"Auto-fix state scope_amendments[{index}]"
        if not isinstance(item, ScopeAmendment):
            raise AssentError(f"{label} is invalid")
        if not isinstance(item.task_id, str) or not _TASK_ID_RE.fullmatch(item.task_id):
            raise AssentError(f"{label} task_id must be a tNNN task id")
        if (not item.finding_fingerprints
                or len(item.finding_fingerprints) != len(item.paths)
                or len(item.paths) != len(item.path_states)):
            raise AssentError(f"{label} scope delta is incomplete")
        if len(set(item.finding_fingerprints)) != len(item.finding_fingerprints):
            raise AssentError(f"{label} has duplicate finding fingerprints")
        if len(set(item.paths)) != len(item.paths):
            raise AssentError(f"{label} has duplicate paths")
        for fingerprint, path, path_state in zip(
                item.finding_fingerprints, item.paths, item.path_states):
            _require_digest(fingerprint, f"{label} finding fingerprint")
            if fingerprint in amended_fingerprints:
                raise AssentError(
                    "Auto-fix state assigns one scope finding to multiple amendments")
            amended_fingerprints.add(fingerprint)
            approved = approved_by_fingerprint.get(fingerprint)
            if (approved is None or approved.task_id != item.task_id
                    or approved.path != path or approved.path_state != path_state):
                raise AssentError(f"{label} does not match its approved scope decision")
            if normalize_finding_path(path) != path:
                raise AssentError(f"{label} path must already be normalized")
            if path_state not in SCOPE_PATH_STATES:
                raise AssentError(f"{label} path_state is invalid")
        for name in ("task_before_sha256", "task_after_sha256",
                     "plan_before_sha256", "plan_after_sha256"):
            _require_digest(getattr(item, name), f"{label} {name}")
        if item.task_before_sha256 == item.task_after_sha256:
            raise AssentError(f"{label} must record an actual task change")
        if item.plan_before_sha256 == item.plan_after_sha256:
            raise AssentError(f"{label} must record an actual plan change")

    disposition_seen: set[tuple[str, str]] = set()
    for index, item in enumerate(state.worker_dispositions):
        if not isinstance(item, WorkerDisposition):
            raise AssentError(
                f"Auto-fix state worker_dispositions[{index}] is invalid")
        if not isinstance(item.task_id, str) or not _TASK_ID_RE.fullmatch(item.task_id):
            raise AssentError("Worker disposition task_id must be a tNNN task id")
        _require_digest(item.fingerprint, "Worker disposition fingerprint")
        if item.fingerprint not in ledger:
            raise AssentError("Worker disposition finding is absent from the ledger")
        if item.disposition not in REPAIR_DISPOSITIONS:
            raise AssentError("Worker disposition value is invalid")
        _require_text(item.detail, "Worker disposition detail", MAX_EVIDENCE_LENGTH)
        identity = (item.task_id, item.fingerprint)
        if identity in disposition_seen:
            raise AssentError("Auto-fix state has a duplicate worker disposition")
        disposition_seen.add(identity)

    brief_tasks: set[str] = set()
    for index, item in enumerate(state.repair_briefs):
        if not isinstance(item, RepairBrief):
            raise AssentError(f"Auto-fix state repair_briefs[{index}] is invalid")
        if not isinstance(item.task_id, str) or not _TASK_ID_RE.fullmatch(item.task_id):
            raise AssentError("Repair brief task_id must be a tNNN task id")
        if item.task_id in brief_tasks:
            raise AssentError("Auto-fix state has duplicate task repair briefs")
        brief_tasks.add(item.task_id)
        _require_multiline_text(
            item.brief, "Repair brief", MAX_REVIEW_RECORD_BYTES)
        if not item.finding_fingerprints:
            raise AssentError("Repair brief must cite at least one finding")
        if len(set(item.finding_fingerprints)) != len(item.finding_fingerprints):
            raise AssentError("Repair brief contains duplicate findings")
        for fingerprint in item.finding_fingerprints:
            _require_digest(fingerprint, "Repair-brief finding fingerprint")
            if fingerprint not in ledger:
                raise AssentError("Repair brief cites a finding absent from the ledger")

    assignment_tasks: set[str] = set()
    for index, item in enumerate(state.repair_round_assignments):
        label = f"Auto-fix state repair_round_assignments[{index}]"
        if not isinstance(item, RepairRoundAssignment):
            raise AssentError(f"{label} is invalid")
        if not isinstance(item.task_id, str) or not _TASK_ID_RE.fullmatch(item.task_id):
            raise AssentError(f"{label} task_id must be a tNNN task id")
        if item.task_id in assignment_tasks:
            raise AssentError(
                "Auto-fix state has duplicate repair-round task assignments")
        assignment_tasks.add(item.task_id)
        for name in ("adapter", "model", "effort"):
            _require_text(getattr(item, name), f"{label} {name}", 1024)
        if type(item.attempted) is not bool:
            raise AssentError(f"{label} attempted must be a boolean")

    for index, item in enumerate(state.plan_digest_transitions):
        if not isinstance(item, PlanDigestTransition):
            raise AssentError(
                f"Auto-fix state plan_digest_transitions[{index}] is invalid")
        _require_digest(item.before_sha256, "Plan transition before_sha256")
        _require_digest(item.after_sha256, "Plan transition after_sha256")
        if item.before_sha256 == item.after_sha256:
            raise AssentError("Plan digest transition must record an actual change")

    for index, item in enumerate(state.review_transitions):
        if not isinstance(item, ReviewTransition):
            raise AssentError(
                f"Auto-fix state review_transitions[{index}] is invalid")
        _require_digest(item.fingerprint, "Review-transition fingerprint")
        if item.fingerprint not in ledger:
            raise AssentError("Review transition cites a finding absent from the ledger")
        if item.transition not in REVIEW_TRANSITION_KINDS:
            raise AssentError("Review transition kind is invalid")
        if item.transition == "initial":
            if item.prior_fingerprint is not None or item.transition_evidence is not None:
                raise AssentError("Initial review transition cannot carry recheck evidence")
        elif item.transition == "still_present":
            _require_digest(item.prior_fingerprint,
                            "Review-transition prior_fingerprint")
            _require_text(item.transition_evidence,
                          "Review-transition evidence", MAX_EVIDENCE_LENGTH)
            if item.prior_fingerprint != item.fingerprint:
                raise AssentError("Still-present review transition must retain identity")
        else:
            if item.prior_fingerprint is not None:
                raise AssentError("New review transition cannot cite a prior fingerprint")
            _require_text(item.transition_evidence,
                          "Review-transition evidence", MAX_EVIDENCE_LENGTH)

    if debt_fingerprints and state.review_stage == "recheck":
        for fingerprint in debt_fingerprints:
            lineage = [
                item.transition for item in state.review_transitions
                if item.fingerprint == fingerprint]
            if (not lineage or lineage[0] != "initial"
                    or lineage[-1] != "still_present"):
                raise AssentError(
                    "Recheck may retain only initial eligible technical debt "
                    "with a still-present transition")

    observed_seen: set[tuple[str, tuple[str, ...]]] = set()
    for index, observed in enumerate(state.observed_states):
        if not isinstance(observed, ObservedState):
            raise AssentError(f"Auto-fix state observed_states[{index}] is invalid")
        _require_tree(observed.source_tree,
                      f"Auto-fix state observed_states[{index}] source_tree")
        finding_seen: set[str] = set()
        for fingerprint in observed.finding_fingerprints:
            _require_digest(fingerprint, "Observed-state finding fingerprint")
            if fingerprint in finding_seen:
                raise AssentError(
                    f"Auto-fix state observed_states[{index}] has duplicate findings")
            finding_seen.add(fingerprint)
            if fingerprint not in ledger:
                raise AssentError(
                    "Auto-fix state observed finding is absent from the finding ledger")
        identity = (observed.source_tree, observed.finding_fingerprints)
        if identity in observed_seen:
            raise AssentError("Auto-fix state has a duplicate observed state")
        observed_seen.add(identity)

    profiles: set[tuple[str, str, str]] = set()
    for index, profile in enumerate(state.consumed_fixer_profiles):
        if not isinstance(profile, FixerProfile):
            raise AssentError(
                f"Auto-fix state consumed_fixer_profiles[{index}] is invalid")
        identity = tuple(
            _require_text(value,
                          f"Auto-fix state consumed_fixer_profiles[{index}] {name}",
                          1024)
            for name, value in (("adapter", profile.adapter),
                                ("model", profile.model),
                                ("effort", profile.effort))
        )
        if identity in profiles:
            raise AssentError("Auto-fix state has a duplicate consumed fixer profile")
        profiles.add(identity)
    for item in state.repair_round_assignments:
        identity = (item.adapter, item.model, item.effort)
        if identity not in profiles:
            raise AssentError(
                "Repair-round assignment profile is absent from consumed history")
    return state


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(toml_string(value) for value in values) + "]"


def _state_text(state: AutoFixState) -> str:
    state = _validate_state(state)
    text = (
        f"version = {state.version}\n"
        f"source_tree = {toml_string(state.source_tree)}\n"
        f"task_plan_sha256 = {toml_string(state.task_plan_sha256)}\n"
        f"review_prompt_sha256 = {toml_string(state.review_prompt_sha256)}\n"
        f"reviewer_adapter = {toml_string(state.reviewer_adapter)}\n"
        f"reviewer_model = {toml_string(state.reviewer_model)}\n"
        f"reviewer_effort = {toml_string(state.reviewer_effort)}\n"
        f"phase = {toml_string(state.phase)}\n"
        f"verdict = {toml_string(state.verdict)}\n"
        f"review_context = {toml_string(state.review_context)}\n"
        f"review_stage = {toml_string(state.review_stage)}\n"
        f"failure_trigger = {toml_string(state.failure_trigger or '')}\n"
        "current_finding_fingerprints = "
        f"{_toml_array(state.current_finding_fingerprints)}\n"
    )
    if not state.findings:
        text += "findings = []\n"
    if not state.observed_states:
        text += "observed_states = []\n"
    if not state.consumed_fixer_profiles:
        text += "consumed_fixer_profiles = []\n"
    for name in ("reviewer_recommendations", "approved_scope_additions",
                 "scope_amendments", "worker_dispositions", "repair_briefs",
                 "repair_round_assignments", "plan_digest_transitions",
                 "review_transitions"):
        if not getattr(state, name):
            text += f"{name} = []\n"
    for finding in state.findings:
        text += (
            "\n[[findings]]\n"
            f"fingerprint = {toml_string(finding.fingerprint)}\n"
            f"kind = {toml_string(finding.kind)}\n"
            f"task_id = {toml_string(finding.task_id or '')}\n"
            f"path = {toml_string(finding.path)}\n"
            f"summary = {toml_string(finding.summary)}\n"
            f"evidence = {toml_string(finding.evidence)}\n"
            f"recommendation = {toml_string(finding.recommendation)}\n"
            "scope_addition_path = "
            f"{toml_string(finding.scope_addition_path or '')}\n"
            "scope_addition_path_state = "
            f"{toml_string(finding.scope_addition_path_state or '')}\n"
        )
    for observed in state.observed_states:
        text += (
            "\n[[observed_states]]\n"
            f"source_tree = {toml_string(observed.source_tree)}\n"
            "finding_fingerprints = "
            f"{_toml_array(observed.finding_fingerprints)}\n"
        )
    for profile in state.consumed_fixer_profiles:
        text += (
            "\n[[consumed_fixer_profiles]]\n"
            f"adapter = {toml_string(profile.adapter)}\n"
            f"model = {toml_string(profile.model)}\n"
            f"effort = {toml_string(profile.effort)}\n"
        )
    for item in state.reviewer_recommendations:
        text += (
            "\n[[reviewer_recommendations]]\n"
            f"fingerprint = {toml_string(item.fingerprint)}\n"
            f"recommendation = {toml_string(item.recommendation)}\n"
        )
    for item in state.approved_scope_additions:
        text += (
            "\n[[approved_scope_additions]]\n"
            f"fingerprint = {toml_string(item.fingerprint)}\n"
            f"task_id = {toml_string(item.task_id)}\n"
            f"path = {toml_string(item.path)}\n"
            f"path_state = {toml_string(item.path_state)}\n"
        )
    for item in state.scope_amendments:
        text += (
            "\n[[scope_amendments]]\n"
            "finding_fingerprints = "
            f"{_toml_array(item.finding_fingerprints)}\n"
            f"task_id = {toml_string(item.task_id)}\n"
            f"paths = {_toml_array(item.paths)}\n"
            f"path_states = {_toml_array(item.path_states)}\n"
            f"task_before_sha256 = {toml_string(item.task_before_sha256)}\n"
            f"task_after_sha256 = {toml_string(item.task_after_sha256)}\n"
            f"plan_before_sha256 = {toml_string(item.plan_before_sha256)}\n"
            f"plan_after_sha256 = {toml_string(item.plan_after_sha256)}\n"
        )
    for item in state.worker_dispositions:
        text += (
            "\n[[worker_dispositions]]\n"
            f"task_id = {toml_string(item.task_id)}\n"
            f"fingerprint = {toml_string(item.fingerprint)}\n"
            f"disposition = {toml_string(item.disposition)}\n"
            f"detail = {toml_string(item.detail)}\n"
        )
    for item in state.repair_briefs:
        text += (
            "\n[[repair_briefs]]\n"
            f"task_id = {toml_string(item.task_id)}\n"
            "finding_fingerprints = "
            f"{_toml_array(item.finding_fingerprints)}\n"
            f"brief = {toml_string(item.brief)}\n"
        )
    for item in state.repair_round_assignments:
        text += (
            "\n[[repair_round_assignments]]\n"
            f"task_id = {toml_string(item.task_id)}\n"
            f"adapter = {toml_string(item.adapter)}\n"
            f"model = {toml_string(item.model)}\n"
            f"effort = {toml_string(item.effort)}\n"
            f"attempted = {'true' if item.attempted else 'false'}\n"
        )
    for item in state.plan_digest_transitions:
        text += (
            "\n[[plan_digest_transitions]]\n"
            f"before_sha256 = {toml_string(item.before_sha256)}\n"
            f"after_sha256 = {toml_string(item.after_sha256)}\n"
        )
    for item in state.review_transitions:
        text += (
            "\n[[review_transitions]]\n"
            f"fingerprint = {toml_string(item.fingerprint)}\n"
            f"transition = {toml_string(item.transition)}\n"
            f"prior_fingerprint = {toml_string(item.prior_fingerprint or '')}\n"
            "transition_evidence = "
            f"{toml_string(item.transition_evidence or '')}\n"
        )
    return text


def write_auto_fix_state(path: str | Path, state: AutoFixState) -> None:
    """Validate and atomically replace the derived state artifact."""
    atomic_write_text(Path(path), _state_text(state))


def _table_list(data: dict, key: str, label: str) -> list[dict]:
    value = data[key]
    if not isinstance(value, list):
        raise AssentError(f"{label} must be an array of tables")
    if not all(isinstance(item, dict) for item in value):
        raise AssentError(f"{label} entries must be tables")
    return value


def read_auto_fix_state(path: str | Path) -> AutoFixState:
    """Read the state fail-closed; malformed derived memory is never ignored."""
    path = Path(path)
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as e:
        raise AssentError(f"Auto-fix state not found: {path}") from e
    except OSError as e:
        raise AssentError(f"Unable to read auto-fix state {path}: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AssentError(f"Auto-fix state is not valid TOML ({path}): {e}") from e
    _require_exact_keys(data, _STATE_KEYS, "Auto-fix state")

    findings: list[PersistedFinding] = []
    for index, item in enumerate(_table_list(data, "findings",
                                              "Auto-fix state findings")):
        _require_exact_keys(item, _PERSISTED_FINDING_KEYS,
                            f"Auto-fix state findings[{index}]")
        item = dict(item)
        if item["task_id"] == "":
            item["task_id"] = None
        if item["scope_addition_path"] == "":
            item["scope_addition_path"] = None
        if item["scope_addition_path_state"] == "":
            item["scope_addition_path_state"] = None
        try:
            findings.append(PersistedFinding(**item))
        except TypeError as e:
            raise AssentError(
                f"Auto-fix state findings[{index}] has an invalid structure: {e}") from e

    observed: list[ObservedState] = []
    for index, item in enumerate(_table_list(
            data, "observed_states", "Auto-fix state observed_states")):
        _require_exact_keys(item, _OBSERVED_STATE_KEYS,
                            f"Auto-fix state observed_states[{index}]")
        try:
            observed.append(ObservedState(**item))
        except TypeError as e:
            raise AssentError(
                f"Auto-fix state observed_states[{index}] has an invalid structure: {e}") from e

    profiles: list[FixerProfile] = []
    for index, item in enumerate(_table_list(
            data, "consumed_fixer_profiles",
            "Auto-fix state consumed_fixer_profiles")):
        _require_exact_keys(item, _FIXER_PROFILE_KEYS,
                            f"Auto-fix state consumed_fixer_profiles[{index}]")
        try:
            profiles.append(FixerProfile(**item))
        except TypeError as e:
            raise AssentError(
                "Auto-fix state consumed_fixer_profiles"
                f"[{index}] has an invalid structure: {e}") from e

    def records(key: str, keys: set[str], record_type: type) -> list:
        result = []
        for index, item in enumerate(_table_list(data, key, f"Auto-fix state {key}")):
            _require_exact_keys(item, keys, f"Auto-fix state {key}[{index}]")
            values = dict(item)
            if record_type is ReviewTransition:
                for nullable in ("prior_fingerprint", "transition_evidence"):
                    if values[nullable] == "":
                        values[nullable] = None
            try:
                result.append(record_type(**values))
            except TypeError as e:
                raise AssentError(
                    f"Auto-fix state {key}[{index}] has an invalid structure: {e}") from e
        return result

    recommendations = records(
        "reviewer_recommendations", _RECOMMENDATION_KEYS,
        ReviewerRecommendation)
    scope_additions = records(
        "approved_scope_additions", _APPROVED_SCOPE_ADDITION_KEYS,
        ApprovedScopeAddition)
    scope_amendments = records(
        "scope_amendments", _SCOPE_AMENDMENT_KEYS, ScopeAmendment)
    dispositions = records(
        "worker_dispositions", _WORKER_DISPOSITION_KEYS, WorkerDisposition)
    briefs = records("repair_briefs", _REPAIR_BRIEF_KEYS, RepairBrief)
    assignments = records(
        "repair_round_assignments", _REPAIR_ROUND_ASSIGNMENT_KEYS,
        RepairRoundAssignment)
    plan_transitions = records(
        "plan_digest_transitions", _PLAN_DIGEST_TRANSITION_KEYS,
        PlanDigestTransition)
    review_transitions = records(
        "review_transitions", _REVIEW_TRANSITION_KEYS, ReviewTransition)

    scalar = dict(data)
    del scalar["findings"]
    del scalar["observed_states"]
    del scalar["consumed_fixer_profiles"]
    del scalar["reviewer_recommendations"]
    del scalar["approved_scope_additions"]
    del scalar["scope_amendments"]
    del scalar["worker_dispositions"]
    del scalar["repair_briefs"]
    del scalar["repair_round_assignments"]
    del scalar["plan_digest_transitions"]
    del scalar["review_transitions"]
    if scalar["failure_trigger"] == "":
        scalar["failure_trigger"] = None
    try:
        state = AutoFixState(
            findings=tuple(findings), observed_states=tuple(observed),
            consumed_fixer_profiles=tuple(profiles),
            reviewer_recommendations=tuple(recommendations),
            approved_scope_additions=tuple(scope_additions),
            scope_amendments=tuple(scope_amendments),
            worker_dispositions=tuple(dispositions), repair_briefs=tuple(briefs),
            repair_round_assignments=tuple(assignments),
            plan_digest_transitions=tuple(plan_transitions),
            review_transitions=tuple(review_transitions), **scalar)
    except TypeError as e:
        raise AssentError(f"Auto-fix state has an invalid structure: {e}") from e
    return _validate_state(state)


def auto_fix_state_is_fresh(
        state: AutoFixState, *, source_tree: str, task_plan_sha256: str,
        review_prompt_sha256: str, reviewer_adapter: str,
        reviewer_model: str, reviewer_effort: str,
        review_context: str = "completed_folder",
        failure_trigger: str | None = None) -> bool:
    """True only when an exact PASS can be reused without another review."""
    _validate_state(state)
    return (
        state.verdict == "PASS"
        and state.review_context == review_context
        and state.failure_trigger == failure_trigger
        and state.source_tree == source_tree
        and state.task_plan_sha256 == task_plan_sha256
        and state.review_prompt_sha256 == review_prompt_sha256
        and state.reviewer_adapter == reviewer_adapter
        and state.reviewer_model == reviewer_model
        and state.reviewer_effort == reviewer_effort
    )


def state_for_review(
        record: ReviewRecord, *, source_tree: str, task_plan_sha256: str,
        review_prompt_sha256: str, reviewer_adapter: str,
        reviewer_model: str, reviewer_effort: str,
        previous: AutoFixState | None = None,
        review_context: str = "completed_folder",
        review_stage: str | None = None,
        failure_trigger: str | None = None,
        worker_dispositions: tuple[WorkerDisposition, ...] | None = None,
        repair_briefs: tuple[RepairBrief, ...] | None = None,
        repair_round_assignments: tuple[RepairRoundAssignment, ...] | None = None,
        plan_digest_transitions: tuple[PlanDigestTransition, ...] | None = None,
        approved_scope_additions: tuple[ApprovedScopeAddition, ...] | None = None,
        scope_amendments: tuple[ScopeAmendment, ...] | None = None,
        enforce_transitions: bool = True) -> AutoFixState:
    """Build the next durable state while retaining prior finding evidence."""
    record = _validate_review_record(record)
    explicit_stage = review_stage is not None
    if review_stage is None:
        review_stage = "recheck" if previous is not None else "initial"
    if (review_stage == "recheck" and previous is not None and not explicit_stage
            and all(item.transition == "initial" for item in record.findings)):
        # Compatibility for callers predating transition-aware reviewer prompts.
        # Explicit version-4 rechecks never receive this scheduler-owned upgrade.
        prior_current = set(previous.current_finding_fingerprints)
        upgraded = []
        for item in record.findings:
            fingerprint = finding_fingerprint(item)
            if fingerprint in prior_current:
                upgraded.append(replace(
                    item, transition="still_present",
                    prior_fingerprint=fingerprint,
                    transition_evidence=item.evidence))
            else:
                upgraded.append(replace(
                    item, transition="newly_exposed",
                    transition_evidence=(
                        "Legacy reviewer output first exposed this blocker after repair.")))
        record = ReviewRecord(record.verdict, tuple(upgraded))
    if enforce_transitions:
        # ``previous`` also carries the folder's cumulative history forward.
        # An initial review continues that history but claims no transition
        # lineage, so it validates against no prior findings.
        record = validate_review_transitions(
            record, review_stage=review_stage,
            previous=None if review_stage == "initial" else previous)
    prior_findings = previous.findings if previous is not None else ()
    prior_observed = previous.observed_states if previous is not None else ()
    prior_profiles = previous.consumed_fixer_profiles if previous is not None else ()
    prior_scope = previous.approved_scope_additions if previous is not None else ()
    prior_amendments = previous.scope_amendments if previous is not None else ()
    prior_review_transitions = previous.review_transitions if previous is not None else ()

    ledger = {finding.fingerprint: finding for finding in prior_findings}
    current: list[str] = []
    for finding in record.findings:
        persisted = persisted_finding(finding)
        ledger.setdefault(persisted.fingerprint, persisted)
        current.append(persisted.fingerprint)
    current_tuple = tuple(current)
    observed = ObservedState(source_tree, current_tuple)
    observations = prior_observed
    if observed not in observations:
        observations += (observed,)
    recommendations = tuple(
        ReviewerRecommendation(fingerprint, ledger[fingerprint].recommendation)
        for fingerprint in current_tuple)
    if approved_scope_additions is None:
        additions = list(prior_scope)
        known_additions = {(item.task_id, item.path) for item in additions}
        for fingerprint in current_tuple:
            finding = ledger[fingerprint]
            if finding.scope_addition_path is None or finding.task_id is None:
                continue
            identity = (finding.task_id, finding.scope_addition_path)
            if identity not in known_additions:
                additions.append(ApprovedScopeAddition(
                    fingerprint, finding.task_id, finding.scope_addition_path,
                    finding.scope_addition_path_state or ""))
                known_additions.add(identity)
        approved_scope_additions = tuple(additions)
    if scope_amendments is None:
        scope_amendments = prior_amendments
    if repair_round_assignments is None:
        # A reviewer decision completes the preceding mutation round. Its
        # profiles remain in the cumulative history, while only a live
        # REPAIRING boundary retains concrete assignments.
        repair_round_assignments = ()
    transitions = prior_review_transitions + tuple(
        ReviewTransition(
            finding_fingerprint(item), item.transition, item.prior_fingerprint,
            item.transition_evidence)
        for item in record.findings)
    if plan_digest_transitions is None:
        plan_digest_transitions = (
            previous.plan_digest_transitions if previous is not None else ())
        if (previous is not None
                and previous.task_plan_sha256 != task_plan_sha256):
            plan_digest_transitions += (PlanDigestTransition(
                previous.task_plan_sha256, task_plan_sha256),)
    state = AutoFixState(
        version=AUTO_FIX_STATE_VERSION,
        source_tree=source_tree,
        task_plan_sha256=task_plan_sha256,
        review_prompt_sha256=review_prompt_sha256,
        reviewer_adapter=reviewer_adapter,
        reviewer_model=reviewer_model,
        reviewer_effort=reviewer_effort,
        phase="COMPLETE" if record.verdict == "PASS" else "NEEDS_REPAIR",
        verdict=record.verdict,
        current_finding_fingerprints=current_tuple,
        findings=tuple(ledger.values()),
        observed_states=observations,
        consumed_fixer_profiles=prior_profiles,
        review_context=review_context,
        review_stage=review_stage,
        failure_trigger=failure_trigger,
        reviewer_recommendations=recommendations,
        approved_scope_additions=approved_scope_additions,
        scope_amendments=scope_amendments,
        worker_dispositions=(
            worker_dispositions if worker_dispositions is not None
            else previous.worker_dispositions if previous is not None else ()),
        repair_briefs=(
            repair_briefs if repair_briefs is not None
            else previous.repair_briefs if previous is not None else ()),
        repair_round_assignments=repair_round_assignments,
        plan_digest_transitions=plan_digest_transitions,
        review_transitions=transitions,
    )
    return _validate_state(state)


read_state = read_auto_fix_state
write_state = write_auto_fix_state
state_is_fresh = auto_fix_state_is_fresh
