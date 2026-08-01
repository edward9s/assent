"""Provider-neutral review records and durable state for folder auto-fix.

This module owns data validation and persistence only.  Adapters remain
responsible for running vendor CLIs, while later scheduler code can consume the
validated records without trusting reviewer-chosen identities.
"""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from assent import AssentError
from assent.config import Config
from assent.verification_common import (DIGEST_RE, OID_RE, atomic_write_text,
                                        toml_string)

AUTO_FIX_STATE_NAME = "_auto_fix.toml"
AUTO_FIX_STATE_VERSION = 1
REVIEW_RECORD_TYPE = "assent.auto_fix_review"
REVIEW_VERDICTS = frozenset({"PASS", "FAIL"})

MAX_REVIEW_OUTPUT_BYTES = 1_048_576
MAX_REVIEW_RECORD_BYTES = 262_144
MAX_FINDINGS = 100
MAX_PATH_LENGTH = 1024
MAX_SUMMARY_LENGTH = 500
MAX_EVIDENCE_LENGTH = 16_000
_TASK_ID_RE = re.compile(r"^t\d{3}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_REVIEW_KEYS = {"type", "verdict", "findings"}
_FINDING_KEYS = {"task_id", "path", "summary", "evidence"}
_STATE_KEYS = {
    "version", "source_tree", "task_plan_sha256", "review_prompt_sha256",
    "reviewer_adapter", "reviewer_model", "reviewer_effort", "verdict",
    "current_finding_fingerprints", "findings", "observed_states",
    "consumed_fixer_profiles",
}
_PERSISTED_FINDING_KEYS = {
    "fingerprint", "task_id", "path", "summary", "evidence",
}
_OBSERVED_STATE_KEYS = {"source_tree", "finding_fingerprints"}
_FIXER_PROFILE_KEYS = {"adapter", "model", "effort"}


@dataclass(frozen=True)
class ReviewFinding:
    """One blocking issue stated by a reviewer, before scheduler fingerprinting."""

    task_id: str | None
    path: str
    summary: str
    evidence: str


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
    task_id: str | None
    path: str
    summary: str
    evidence: str

    @property
    def finding(self) -> ReviewFinding:
        return ReviewFinding(self.task_id, self.path, self.summary, self.evidence)


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
class AutoFixState:
    """Deletable runtime memory for one folder's bounded review/repair loop."""

    version: int
    source_tree: str
    task_plan_sha256: str
    review_prompt_sha256: str
    reviewer_adapter: str
    reviewer_model: str
    reviewer_effort: str
    verdict: str
    current_finding_fingerprints: tuple[str, ...]
    findings: tuple[PersistedFinding, ...]
    observed_states: tuple[ObservedState, ...]
    consumed_fixer_profiles: tuple[FixerProfile, ...]

    def __post_init__(self) -> None:
        for name in ("current_finding_fingerprints", "findings",
                     "observed_states", "consumed_fixer_profiles"):
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


def _validate_finding(finding: ReviewFinding, label: str) -> ReviewFinding:
    if not isinstance(finding, ReviewFinding):
        raise AssentError(f"{label} must be a review finding")
    task_id = finding.task_id
    if task_id is not None and (not isinstance(task_id, str)
                                or not _TASK_ID_RE.fullmatch(task_id)):
        raise AssentError(f"{label} task_id must be null or a tNNN task id")
    return ReviewFinding(
        task_id=task_id,
        path=normalize_finding_path(finding.path),
        summary=_require_text(finding.summary, f"{label} summary",
                              MAX_SUMMARY_LENGTH),
        evidence=_require_text(finding.evidence, f"{label} evidence",
                               MAX_EVIDENCE_LENGTH),
    )


def finding_fingerprint(finding: ReviewFinding) -> str:
    """Compute the stable identity; reviewers never supply this value."""
    finding = _validate_finding(finding, "Review finding")
    canonical = json.dumps(
        {"task_id": finding.task_id, "path": finding.path,
         "summary": finding.summary, "evidence": finding.evidence},
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
            {"task_id": finding.task_id, "path": finding.path,
             "summary": finding.summary, "evidence": finding.evidence}
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
        findings.append(ReviewFinding(
            task_id=raw["task_id"], path=raw["path"],
            summary=raw["summary"], evidence=raw["evidence"],
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
        fingerprint=finding_fingerprint(finding), task_id=finding.task_id,
        path=finding.path, summary=finding.summary, evidence=finding.evidence,
    )


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
    if not isinstance(state.verdict, str) or state.verdict not in REVIEW_VERDICTS:
        raise AssentError("Auto-fix state verdict must be PASS or FAIL")
    for name in ("current_finding_fingerprints", "findings",
                 "observed_states", "consumed_fixer_profiles"):
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
        f"verdict = {toml_string(state.verdict)}\n"
        "current_finding_fingerprints = "
        f"{_toml_array(state.current_finding_fingerprints)}\n"
    )
    for finding in state.findings:
        text += (
            "\n[[findings]]\n"
            f"fingerprint = {toml_string(finding.fingerprint)}\n"
            f"task_id = {toml_string(finding.task_id or '')}\n"
            f"path = {toml_string(finding.path)}\n"
            f"summary = {toml_string(finding.summary)}\n"
            f"evidence = {toml_string(finding.evidence)}\n"
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

    scalar = dict(data)
    del scalar["findings"]
    del scalar["observed_states"]
    del scalar["consumed_fixer_profiles"]
    try:
        state = AutoFixState(
            findings=tuple(findings), observed_states=tuple(observed),
            consumed_fixer_profiles=tuple(profiles), **scalar)
    except TypeError as e:
        raise AssentError(f"Auto-fix state has an invalid structure: {e}") from e
    return _validate_state(state)


def auto_fix_state_is_fresh(
        state: AutoFixState, *, source_tree: str, task_plan_sha256: str,
        review_prompt_sha256: str, reviewer_adapter: str,
        reviewer_model: str, reviewer_effort: str) -> bool:
    """True only when an exact PASS can be reused without another review."""
    _validate_state(state)
    return (
        state.verdict == "PASS"
        and state.source_tree == source_tree
        and state.task_plan_sha256 == task_plan_sha256
        and state.review_prompt_sha256 == review_prompt_sha256
        and state.reviewer_adapter == reviewer_adapter
        and state.reviewer_model == reviewer_model
        and state.reviewer_effort == reviewer_effort
    )


read_state = read_auto_fix_state
write_state = write_auto_fix_state
state_is_fresh = auto_fix_state_is_fresh
