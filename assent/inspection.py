"""Read-only inspection of a work folder: report, status, check.

None of these commands starts an AI session, takes the folder lock, or changes
Git state, so they stay usable while a run is in progress.  They answer three
questions about a folder as it stands right now:

- ``report`` renders and writes ``_report.md``, the acceptance meeting's agenda;
- ``status`` prints the progress counts, the checkpoint, the stack, and which
  task would run next;
- ``check`` validates the environment and the plan format -- the condition that
  adjourns a planning meeting.

Aggregation is mechanical work, zero tokens.  The shared pre-session decisions
(effort resolution, adapter capability, assignment rendering, stack state) come
from ``assent.preflight``, which ``assent.engine`` uses too, so a query and a
run can never answer them differently.  This module must not import
``assent.engine``: the report is written from inside a run as a best-effort
side effect, and the dependency runs that way only.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from assent import AssentError, auto_fix, contracts, gitops, usage
from assent.adapters import Adapter, get_adapter
from assent.config import PROJECT_LAYER, Config, WorkflowPlanStep
from assent.folderdeps import parse_folder_dependencies
from assent.folder_verification import receipt_report_lines
from assent.modeling import literal_value
from assent.plan import Plan, Task, read_entries
from assent.preflight import (GIT_REQUIRED_MESSAGE, SessionIdentity,
                              capability_errors, has_git_marker,
                              print_task_assignments, resolve_effort,
                              resolve_requested_effort, resolve_stack_state,
                              resolve_task_assignments,
                              worktree_configuration_errors)


_TASK_STATUS_LINE_RE = re.compile(
    rb'^(\s*status\s*=\s*")(TODO|WIP|DONE|BLOCKED|SKIP)'
    rb'("[^\r\n]*)(\r?\n)?$')
_TASK_STATUSES = frozenset(("TODO", "WIP", "DONE", "BLOCKED", "SKIP"))


def _git_read(root, *args: str) -> str | None:
    """Read-only git query; a missing git or a non-zero exit returns None (used by
    status/check, so it never raises a traceback)."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(root),
            capture_output=True, encoding="utf-8", errors="replace")
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _query_git_root(cfg: Config) -> Path:
    """When a valid worktree already exists, read git info from the isolated branch instead."""
    if cfg.source_root is not None:
        return cfg.root
    candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    top = _git_read(candidate, "rev-parse", "--show-toplevel")
    if top and Path(top).resolve() == candidate.resolve():
        return candidate
    return cfg.root


def _stack_report_lines(cfg: Config, plan: Plan) -> list[str]:
    """Describe the currently derived stack without authorizing any action."""
    if all(t.status in ("DONE", "SKIP") for t in plan.tasks):
        return ["Stack base: not applicable (folder complete)"]
    try:
        state = resolve_stack_state(cfg)
    except AssentError as e:
        return [f"Stack base: unavailable ({e})"]
    upstream = state.base.speculative_upstream
    if upstream is None:
        return ["Stack base: current target main",
                "Speculative upstream: none (all direct upstreams accepted)"]
    return [f"Stack base: {state.base.resolved_base}",
            f"Speculative upstream: {upstream.folder} @ {upstream.tip} (unaccepted)"]


# --------------------------------------------------------------------------- #
# report: zero-token human-readable report (the acceptance meeting's agenda)
# --------------------------------------------------------------------------- #
def render_report(cfg: Config, plan: Plan,
                  now: Callable[[], datetime] | None = None) -> str:
    """Aggregate the t/r files and git info into a one-page plain-text report. Aggregation is
    mechanical work, zero tokens."""
    if now is None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731
    counts = Counter(t.status for t in plan.tasks)

    git_root = _query_git_root(cfg)
    checkpoints: dict[str, str] = {}
    log = _git_read(git_root, "log", "--pretty=%h\t%s")
    if log:
        for line in log.splitlines():
            h, _, subject = line.partition("\t")
            for t in plan.tasks:
                prefix = f"auto({cfg.tasks_name}/{t.id}): "
                if t.id not in checkpoints and subject.startswith(prefix):
                    checkpoints[t.id] = h

    branch = _git_read(git_root, "branch", "--show-current") or "N/A"
    stamp = now().astimezone().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "Run report (_report.md; auto-generated by assent, do not edit by hand; regenerate: assent report)",
        "=" * 60,
        f"Plan folder: {cfg.tasks_name}",
        f"Generated at: {stamp}",
        f"Branch: {branch}",
        f"Progress: DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
        f"WIP {counts.get('WIP', 0)} / TODO {counts.get('TODO', 0)} / "
        f"SKIP {counts.get('SKIP', 0)} ({len(plan.tasks)} total)",
        *_stack_report_lines(cfg, plan),
        "",
    ]
    for t in plan.tasks:
        mark = checkpoints.get(t.id)
        lines.append(f"{t.id}  {t.status:<8} {t.title}"
                     + (f"  [{mark}]" if mark else ""))
        if t.status in ("BLOCKED", "WIP"):
            entries = read_entries(t.journal_path)
            if entries:
                last = entries[-1]
                summary = str(last.get("summary", "")).strip()
                by = last.get("by", "?")
                if summary:
                    lines.append(f"      last journal ({by}): {summary}")
    blocked = [t for t in plan.tasks if t.status == "BLOCKED"]
    if blocked:
        pending = _pending_auto_fix_for_blocked(cfg, plan, blocked)
        if pending is None:
            lines += ["", "To decide: compare each BLOCKED task's r file and checkpoint commit, "
                      "edit the task file and set status back to TODO to continue, or mark SKIP to abandon."]
        else:
            state, task_ids = pending
            lifecycle = (
                "blocked adjudication"
                if state.review_context == "blocked_adjudication"
                else "repair")
            lines += ["", "To decide: pending autonomous " + lifecycle
                      + " covers BLOCKED task(s) " + ", ".join(task_ids)
                      + "; inspect the Plan auto-fix lifecycle below and defer "
                        "human task-file disposition until it reaches a terminal outcome."]
    lines += ["", *usage.report_lines(cfg.assent_dir, cfg.tasks_name),
              "", *auto_fix_report_lines(cfg, plan),
              "", *receipt_report_lines(cfg)]
    return "\n".join(lines) + "\n"


def write_report(cfg: Config, plan: Plan,
                 now: Callable[[], datetime] | None = None) -> Path:
    """Write the report to the task folder's _report.md (a runtime artifact, not version-controlled)."""
    path = cfg.tasks_dir / "_report.md"
    path.write_text(render_report(cfg, plan, now), encoding="utf-8")
    _write_technical_debt_report(cfg)
    return path


_PENDING_AUTO_FIX_PHASES = frozenset({"NEEDS_REPAIR", "REPAIRING", "AWAITING_REVIEW"})


def _auto_fix_binding_reasons(
        cfg: Config, plan: Plan, state: auto_fix.AutoFixState
        ) -> tuple[list[str], str | None]:
    """Return current binding drift without starting work or changing state."""
    reasons: list[str] = []
    current_tree: str | None = None
    try:
        # The review states the folder's own source, which lives on its
        # isolated branch, so the comparison reads that worktree whenever one
        # exists -- reading the main tree from a report invocation would call
        # every live folder's evidence stale.
        current_tree = gitops.tree_of(_query_git_root(cfg), "HEAD")
    except AssentError as e:
        reasons.append(f"current source identity unavailable: {e}")
    else:
        if current_tree != state.source_tree:
            reasons.append("source tree changed")

    try:
        if not _task_plan_matches_state(plan, state):
            reasons.append("task contracts changed")
    except AssentError as e:
        reasons.append(f"task contracts unavailable: {e}")

    steps = tuple(
        step for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep))
    position = state.reviewer_step_index
    stored_reviewer = (
        state.reviewer_role, state.reviewer_adapter,
        state.reviewer_model, state.reviewer_effort)
    if (position >= len(steps)
            or (steps[position].role, steps[position].adapter,
                steps[position].requested_model,
                steps[position].requested_effort) != stored_reviewer):
        reasons.append("workflow role, identity, or step position changed")
    return reasons, current_tree


def _pending_auto_fix_for_blocked(
        cfg: Config, plan: Plan, blocked: list[Task]
        ) -> tuple[auto_fix.AutoFixState, tuple[str, ...]] | None:
    """Find fresh pending auto-fix evidence that owns the displayed BLOCKED tasks."""
    path = auto_fix.auto_fix_state_path(cfg)
    if not path.is_file():
        return None
    try:
        state = auto_fix.read_auto_fix_state(path)
    except AssentError:
        return None
    # Every non-PASS verdict is a pending state: FIXED is an unconfirmed
    # self-repair awaiting its recheck, exactly as FAIL awaits its repair.
    if state.verdict == "PASS" or state.phase not in _PENDING_AUTO_FIX_PHASES:
        return None
    reasons, _current_tree = _auto_fix_binding_reasons(cfg, plan, state)
    if reasons:
        return None
    blocked_ids = {task.id for task in blocked}
    current = set(state.current_finding_fingerprints)
    covered = {
        finding.task_id for finding in state.findings
        if finding.fingerprint in current and finding.task_id in blocked_ids
    }
    if not covered:
        return None
    return state, tuple(sorted(covered))


def auto_fix_report_lines(cfg: Config, plan: Plan) -> list[str]:
    """Return zero-token plan-level auto-fix evidence for the report.

    The state file is derived memory, not an acceptance gate.  A valid state is
    fresh only while the source tree and task contracts it names are still the
    current ones; malformed or no-longer-bound state is shown as stale so a
    human can see that another review is needed.
    """
    path = auto_fix.auto_fix_state_path(cfg)
    if not path.exists():
        return ["Plan auto-fix: NOT RUN (no review state)"]

    try:
        state = auto_fix.read_auto_fix_state(path)
    except AssentError as e:
        return [f"Plan auto-fix: STALE (malformed review state: {e})"]

    reasons, current_tree = _auto_fix_binding_reasons(cfg, plan, state)

    self_fixed = state.self_fixed_unreviewed
    unresolved = state.unresolved_review
    if reasons:
        status = "STALE"
        freshness = "; ".join(reasons)
    elif self_fixed is not None:
        # Distinct from all four other states: the code passed every focused
        # gate its own tasks declare, and only independent review confirmation
        # is missing.
        status = "SELF-FIXED, UNREVIEWED"
        freshness = "fresh"
    elif unresolved is not None:
        # The other terminal outcome, and distinct from the self-fixed one: the
        # finite round list ended with a blocker no round resolved, so what a
        # human must decide is the finding itself, not a missing confirmation.
        status = "REVIEW UNRESOLVED, HUMAN DECISION"
        freshness = "fresh"
    elif state.verdict == "PASS":
        status = "PASSED"
        freshness = "fresh"
    else:
        status = "FAILED"
        freshness = "fresh"

    lines = [f"Plan auto-fix: {status} ({freshness})",
             f"  Source tree: {state.source_tree}",
             f"  Phase: {state.phase}",
             f"  Verdict: {state.verdict}",
             f"  Review context: {state.review_context.upper()}",
             f"  Review stage: {state.review_stage.upper()}",
             f"  Original blocker: {_auto_fix_blocker_label(state)}"]
    if current_tree is not None and current_tree != state.source_tree:
        lines.append(f"  Current source tree: {current_tree}")
    if self_fixed is not None:
        lines.append(
            f"  Self-fixed round: {self_fixed.round_index + 1} of "
            f"{self_fixed.rounds_used} "
            f"({self_fixed.adapter}/{self_fixed.model}/{self_fixed.effort}); "
            "no later configured round confirmed the repair")
    if unresolved is not None:
        lines.append(
            f"  Unresolved review round: {unresolved.round_index + 1} of "
            f"{unresolved.rounds_used} "
            f"({unresolved.adapter}/{unresolved.model}/{unresolved.effort}); "
            f"{len(unresolved.finding_fingerprints)} finding(s) no configured "
            "round resolved")

    if state.repair_briefs:
        for brief in state.repair_briefs:
            evidence = _repair_brief_section(brief.brief,
                                              "Original blocker evidence:",
                                              "Focused command evidence:")
            if evidence:
                lines.append(
                    f"  Original blocker evidence ({brief.task_id}): "
                    f"{_compact_report_text(evidence)}")

    ledger = {item.fingerprint: item for item in state.findings}
    lines.append("  Current findings and recommendations:")
    if not state.current_finding_fingerprints:
        lines.append("    - none (PASS / prior findings cleared)")
    else:
        for fingerprint in state.current_finding_fingerprints:
            finding = ledger[fingerprint]
            owner = finding.task_id or "unassigned"
            lines.append(
                f"    - {fingerprint} {owner} {finding.path}: "
                f"{_compact_report_text(finding.summary)}")
            lines.append(
                f"      evidence: {_compact_report_text(finding.evidence)}")
            lines.append(
                f"      recommendation: "
                f"{_compact_report_text(finding.recommendation)}")

    lines.append("  Approved scope additions:")
    if not state.approved_scope_additions:
        lines.append("    - none")
    else:
        for addition in state.approved_scope_additions:
            lines.append(
                f"    - {addition.fingerprint} {addition.task_id}: "
                f"{addition.path} ({addition.path_state})")

    current_scope_findings = [
        item for item in state.findings
        if item.fingerprint in state.current_finding_fingerprints
        and item.kind == "scope_amendment"
    ]
    if state.approved_scope_additions:
        scope_status = "APPROVED (scheduler-owned exact transaction)"
    elif current_scope_findings:
        scope_status = "PENDING (reviewed exact decision not yet applied)"
    else:
        scope_status = "NONE"
    lines.append(f"  Scope amendment: {scope_status}")

    lines.append("  Repair acknowledgements:")
    if not state.worker_dispositions:
        lines.append("    - none recorded")
    else:
        for disposition in state.worker_dispositions:
            lines.append(
                f"    - {disposition.task_id} {disposition.fingerprint}: "
                f"{disposition.disposition}; "
                f"{_compact_report_text(disposition.detail)}")

    lines.append("  Repair briefs:")
    if not state.repair_briefs:
        lines.append("    - none recorded")
    else:
        for brief in state.repair_briefs:
            lines.append(
                f"    - {brief.task_id}: "
                f"{_compact_report_text(brief.brief)}")

    lines.append(
        f"  Workflow step cursor: {state.workflow_step_index}"
        f" (configured steps: {len(cfg.workflow_plan)})")

    lines.append("  Scope amendment transactions:")
    if not state.scope_amendments:
        lines.append("    - none recorded")
    else:
        for amendment in state.scope_amendments:
            paths = ", ".join(amendment.paths)
            lines.append(
                f"    - {amendment.task_id}: {paths}"
                f" ({', '.join(amendment.path_states)})")
            lines.append(
                f"      task contract: {amendment.task_before_sha256}"
                f" -> {amendment.task_after_sha256}")
            lines.append(
                f"      task plan: {amendment.plan_before_sha256}"
                f" -> {amendment.plan_after_sha256}")

    if state.plan_digest_transitions:
        lines.append("  Plan digest transitions:")
        lines.extend(
            f"    - {item.before_sha256} -> {item.after_sha256}"
            for item in state.plan_digest_transitions)
    if state.review_transitions:
        lines.append("  Review finding transitions:")
        lines.extend(
            f"    - {item.fingerprint}: {item.transition}"
            for item in state.review_transitions)

    exhaustion = _auto_fix_exhaustion(plan)
    if self_fixed is not None:
        lines.append(
            "  Terminal: SELF-FIXED, UNREVIEWED (round "
            f"{self_fixed.round_index + 1} of {self_fixed.rounds_used}, "
            f"{self_fixed.adapter}/{self_fixed.model}/{self_fixed.effort}; "
            "every task passed its own focused gate, and acceptance remains "
            "the human accept action)")
    elif unresolved is not None:
        lines.append(
            "  Terminal: REVIEW UNRESOLVED, HUMAN DECISION (round "
            f"{unresolved.round_index + 1} of {unresolved.rounds_used}, "
            f"{unresolved.adapter}/{unresolved.model}/{unresolved.effort}; "
            "the run succeeded, every task keeps the status its own closeout "
            "gave it, and the findings above are the human accept decision)")
    elif exhaustion is not None:
        lines.append(f"  Exhaustion reason: {_compact_report_text(exhaustion)}")
        lines.append("  Terminal: NONZERO / EXHAUSTED")
    elif state.verdict == "PASS":
        lines.append("  Terminal: PASS (acceptance still requires the human accept action)")
    elif state.phase == "REPAIRING":
        lines.append("  Terminal: NONZERO / REPAIRING")
    elif state.phase == "AWAITING_REVIEW":
        lines.append("  Terminal: NONZERO / AWAITING REVIEW")
    elif current_scope_findings:
        lines.append("  Terminal: NONZERO / PENDING SCOPE AMENDMENT")
    elif state.review_context == "blocked_adjudication":
        lines.append("  Terminal: NONZERO / PENDING BLOCKED ADJUDICATION")
    elif state.phase == "NEEDS_REPAIR":
        lines.append("  Terminal: NONZERO / PENDING REPAIR")
    else:
        lines.append("  Terminal: NONZERO / UNRESOLVED FINDINGS")

    if _eligible_technical_debt(state):
        lines.append(
            "  TECHNICAL DEBT REVIEW REQUIRED: read _technical_debt.md before "
            "recommending acceptance and enumerate every item with the human.")
    return lines


_REPORT_TEXT_LIMIT = 360


def _task_plan_digest_for_statuses(
        plan: Plan, statuses: dict[str, str], *, normalize_text: bool = False
        ) -> str | None:
    """Hash current task bytes after replacing only their status lines.

    The scheduler's auto-fix binding includes exact task-contract bytes.  A
    status-only transition is scheduler-owned lifecycle evidence, however, so
    report freshness checks the original completed/blocked status shape while
    still treating every other byte as a structural contract change.
    """
    digest = hashlib.sha256()
    for task in sorted(plan.tasks, key=lambda item: item.path.name):
        status = statuses.get(task.id)
        if status not in _TASK_STATUSES:
            return None
        try:
            data = (task.path.read_text(encoding="utf-8").encode("utf-8")
                    if normalize_text else task.path.read_bytes())
        except OSError as e:
            raise AssentError(
                f"Unable to hash auto-fix task contract {task.path}: {e}") from e
        replacement = status.encode("ascii")
        chunks: list[bytes] = []
        matches = 0
        for line in data.splitlines(keepends=True):
            match = _TASK_STATUS_LINE_RE.match(line)
            if match is None:
                chunks.append(line)
                continue
            matches += 1
            chunks.append(
                match.group(1) + replacement + match.group(3)
                + (match.group(4) or b""))
        if matches != 1:
            return None
        normalized = b"".join(chunks)
        digest.update(task.path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(normalized)).encode("ascii"))
        digest.update(b"\0")
        digest.update(normalized)
        digest.update(b"\0")
    return digest.hexdigest()


def _task_status_baseline(
        plan: Plan, state: auto_fix.AutoFixState) -> dict[str, str]:
    """Infer the status shape bound by the review without trusting status edits.

    Completed-plan reviews bind DONE/SKIP contracts.  A blocked review keeps
    the live status for unaffected tasks, while an exact automatic rework
    journal records the status that preceded its scheduler reset.
    """
    statuses: dict[str, str] = {}
    for task in plan.tasks:
        if state.review_context == "completed_folder":
            statuses[task.id] = "SKIP" if task.status == "SKIP" else "DONE"
        else:
            statuses[task.id] = task.status
        try:
            entries = read_entries(task.journal_path)
        except AssentError:
            continue
        if state.review_context != "blocked_adjudication":
            continue
        for entry in entries:
            if entry.get("event") != "rework_requested":
                continue
            detail = entry.get("detail")
            if not isinstance(detail, str):
                continue
            match = re.search(
                r"(?m)^original status:\s*(TODO|WIP|DONE|BLOCKED|SKIP)\s*$",
                detail)
            if match:
                statuses[task.id] = match.group(1)
    return statuses


def _task_plan_matches_state(
        plan: Plan, state: auto_fix.AutoFixState) -> bool:
    """Accept exact bytes or a scheduler-owned status-only task transition."""
    try:
        exact = auto_fix.sha256_files(task.path for task in plan.tasks)
    except AssentError:
        raise
    if exact == state.task_plan_sha256:
        return True
    current_statuses = {task.id: task.status for task in plan.tasks}
    normalized_exact = _task_plan_digest_for_statuses(
        plan, current_statuses, normalize_text=True)
    if normalized_exact == state.task_plan_sha256:
        return True
    baseline = _task_status_baseline(plan, state)
    normalized = _task_plan_digest_for_statuses(plan, baseline)
    if normalized == state.task_plan_sha256:
        return True
    normalized_text = _task_plan_digest_for_statuses(
        plan, baseline, normalize_text=True)
    return normalized_text == state.task_plan_sha256


def _compact_report_text(value: object, limit: int = _REPORT_TEXT_LIMIT) -> str:
    """Keep durable evidence readable in a zero-token report without losing its identity."""
    text = " ".join(str(value).split())
    if not text:
        return "(none)"
    if len(text) <= limit:
        return text
    return text[:limit - 15].rstrip() + " ... [truncated]"


def _repair_brief_section(brief: str, start: str, end: str) -> str:
    """Extract one bounded section from a scheduler-generated repair brief."""
    if start not in brief:
        return ""
    section = brief.split(start, 1)[1]
    if end in section:
        section = section.split(end, 1)[0]
    return section.strip()


def _auto_fix_blocker_label(state: auto_fix.AutoFixState) -> str:
    if state.failure_trigger == "worker_blocked":
        return "worker BLOCKED durable evidence"
    if state.failure_trigger == "focused_gate_failure":
        return "focused task gate failure durable evidence"
    if state.review_context == "completed_folder":
        return "none (completed-plan review)"
    return "durable blocked-review evidence"


def _auto_fix_exhaustion(plan: Plan) -> str | None:
    """Find the scheduler's durable finite-termination reason without starting work."""
    for task in plan.tasks:
        for entry in read_entries(task.journal_path):
            if entry.get("event") != "auto_fix_exhausted":
                continue
            summary = str(entry.get("summary", "")).strip()
            detail = str(entry.get("detail", "")).strip()
            return summary or detail or "automatic repair profiles exhausted"
    return None


def _eligible_technical_debt(state: auto_fix.AutoFixState) -> list[auto_fix.PersistedFinding]:
    """Return debt first introduced by an INITIAL completed-plan review.

    The ledger intentionally retains resolved findings.  A transition marked
    ``initial`` is the durable proof that the debt entered through the only
    context allowed to introduce it; later rechecks and blocked adjudications
    can only preserve or resolve that entry.
    """
    initial = {
        item.fingerprint for item in state.review_transitions
        if item.transition == "initial"
    }
    return [item for item in state.findings
            if item.kind == "eligible_technical_debt"
            and item.fingerprint in initial]


def _technical_debt_report_text(
        state: auto_fix.AutoFixState, folder: str) -> str:
    """Render the derived human debt agenda from the durable ledger."""
    current = set(state.current_finding_fingerprints)
    approved = {item.fingerprint: item
                for item in state.approved_scope_additions}
    dispositions: dict[str, list[auto_fix.WorkerDisposition]] = {}
    for item in state.worker_dispositions:
        dispositions.setdefault(item.fingerprint, []).append(item)
    transitions: dict[str, list[auto_fix.ReviewTransition]] = {}
    for item in state.review_transitions:
        transitions.setdefault(item.fingerprint, []).append(item)

    lines = [
        "# Technical debt review agenda (auto-generated; do not edit by hand)",
        "",
        "TECHNICAL DEBT REVIEW REQUIRED",
        "",
        f"Plan folder: {folder}",
        "This zero-token agenda preserves eligible debt first introduced by an "
        "INITIAL completed-plan review. It is not task status, acceptance "
        "evidence, or permission to start another repair round.",
    ]
    for index, finding in enumerate(_eligible_technical_debt(state), 1):
        outcome = ("CURRENT / unresolved in the latest review"
                   if finding.fingerprint in current
                   else "RESOLVED / absent from the latest current findings")
        lines.extend([
            "",
            f"## Debt finding {index}: {finding.fingerprint}",
            f"- Task: {finding.task_id or 'unassigned'}",
            f"- Path: {finding.path}",
            f"- Finding: {_compact_report_text(finding.summary)}",
            f"- Evidence: {_compact_report_text(finding.evidence)}",
            f"- Recommendation: {_compact_report_text(finding.recommendation)}",
            f"- Outcome: {outcome}",
        ])
        records = dispositions.get(finding.fingerprint, [])
        lines.append("- Repair dispositions:")
        if records:
            lines.extend(
                f"  - {item.task_id}: {item.disposition}; "
                f"{_compact_report_text(item.detail)}"
                for item in records)
        else:
            lines.append("  - none recorded")
        addition = approved.get(finding.fingerprint)
        if addition is not None:
            lines.append(
                f"- Scope decision: approved exact addition "
                f"{addition.path} ({addition.path_state}) for {addition.task_id}")
        elif finding.scope_addition_path is not None:
            lines.append(
                f"- Scope decision: proposed "
                f"{finding.scope_addition_path} ({finding.scope_addition_path_state}); "
                "no approved transaction recorded")
        else:
            lines.append("- Scope decision: no scope addition")
        history = transitions.get(finding.fingerprint, [])
        lines.append("- Review history: " + ", ".join(
            item.transition for item in history) if history else
                     "- Review history: initial")
    return "\n".join(lines) + "\n"


def _write_technical_debt_report(cfg: Config) -> Path | None:
    """Write the derived debt agenda when the durable state contains eligible debt."""
    state_path = auto_fix.auto_fix_state_path(cfg)
    if not state_path.is_file():
        return None
    try:
        state = auto_fix.read_auto_fix_state(state_path)
    except AssentError:
        return None
    if not _eligible_technical_debt(state):
        return None
    path = cfg.tasks_dir / "_technical_debt.md"
    path.write_text(_technical_debt_report_text(state, cfg.tasks_name),
                    encoding="utf-8")
    return path


def try_write_report(cfg: Config) -> None:
    """Best-effort report update at run wrap-up; a report failure never affects the main
    flow's result or exit code."""
    try:
        write_report(cfg, Plan.parse(cfg.tasks_dir))
    # This is a deliberate best-effort isolation boundary: any ordinary error including
    # permissions, file locks, and content parsing must not mask the task result;
    # KeyboardInterrupt/SystemExit still propagate as usual.
    except Exception:
        pass


def report(cfg: Config) -> int:
    """Subcommand: generate _report.md and print it to the terminal (zero tokens)."""
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder: {e}")
        return 1
    text = render_report(cfg, plan)
    path = write_report(cfg, plan)
    print(text, end="")
    print(f"(written to {path})")
    return 0


# --------------------------------------------------------------------------- #
# status: zero-token progress query
# --------------------------------------------------------------------------- #
def status(cfg: Config) -> int:
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder: {e}")
        return 1

    counts = Counter(t.status for t in plan.tasks)
    print(f"Task folder: {cfg.tasks_dir}")
    print(f"Progress: DONE {counts.get('DONE', 0)} / BLOCKED {counts.get('BLOCKED', 0)} / "
          f"SKIP {counts.get('SKIP', 0)} / WIP {counts.get('WIP', 0)} / "
          f"TODO {counts.get('TODO', 0)} ({len(plan.tasks)} total)")

    git_root = _query_git_root(cfg)
    branch = _git_read(git_root, "branch", "--show-current")
    print(f"Current branch: {branch or 'N/A'}")
    last = _git_read(git_root, "log", "-1", "--grep=^auto(", "--pretty=%h %s")
    print(f"Last checkpoint: {last or '(no auto() commit yet)'}")
    for line in _stack_report_lines(cfg, plan):
        print(line)

    selected = plan.next_task()
    if selected is not None:
        nxt, resumed = selected
        try:
            effort = resolve_effort(cfg, nxt)
            requested_effort = resolve_requested_effort(cfg, nxt.model, effort)
            effort_label = f"{effort} -> {requested_effort}"
        except AssentError as e:
            effort_label = f"unavailable ({e})"
        tag = " (WIP resume)" if resumed else ""
        print(f"Next task: {nxt.id} [{nxt.model} / {effort_label}] "
              f"{nxt.title}{tag}")
    elif counts.get("TODO", 0):
        print("Next task: (TODO remains, but blocked by unfinished prerequisites or a BLOCKED task)")
    else:
        print("Next task: (none, all DONE/BLOCKED/SKIP)")
    return 0


# --------------------------------------------------------------------------- #
# check: zero-token environment and format validation (the meeting's adjourn condition)
# --------------------------------------------------------------------------- #
def _config_source_lines(cfg: Config) -> list[str]:
    """State the layers behind the effective settings, lowest priority first.

    The project file is an optional override and a project locator, so a setup that
    states everything user-wide is complete: naming the absent file as optional keeps
    that ordinary case from reading like a failure.
    """
    parts = [source.layer if source.path is None
             else f"{source.layer} ({source.path})" for source in cfg.sources]
    lines = [f"Config: OK (task folder = {cfg.tasks_name})",
             "Config sources (lowest priority first): " + ", ".join(parts)]
    if not any(source.layer == PROJECT_LAYER for source in cfg.sources):
        lines.append(f"  project override {cfg.assent_dir / 'assent.toml'}: "
                     "absent (optional)")
    return lines


def _contract_lines() -> tuple[bool, list[str]]:
    """Validate both global contracts, reporting each problem on its own line."""
    try:
        errors = contracts.contract_errors()
    except AssentError as e:
        return False, [f"Global contracts: FAIL ({e})"]
    if not errors:
        return True, [f"Global contracts: OK ({contracts.contract_dir()}: "
                      f"{', '.join(contracts.CONTRACT_NAMES)} current)"]
    return False, ["Global contracts: FAIL",
                   *(f"  - {message}" for message in errors),
                   f"  {contracts.CONTRACT_REMEDY}"]


def _assignment_source_lines(
        cfg: Config,
        blocks: list[tuple[str, list[tuple[Task, SessionIdentity]]]]
        ) -> list[str]:
    """Name the layer behind every setting the printed assignments actually used.

    Only the keys the resolution consumed are shown -- the adapter selection, and per
    adapter the model, default-effort and effort translation of each tier in the plan --
    so the provenance answers "why this invocation" without dumping the whole config.
    """
    lines = [f"Setting sources: adapter.name = {cfg.source_of('adapter.name')}"
             f" (active: {', '.join(cfg.adapter_names)})"]
    for adapter_name, assignments in blocks:
        settings = cfg.adapter_settings(adapter_name)
        keys: list[str] = []
        for task, session in assignments:
            literal_model = literal_value(task.model) is not None
            if not literal_model:
                keys.append(f"models.{task.model}")
            if session.effort is None:
                continue
            if task.effort is None and not literal_model:
                keys.append(f"default_effort.{task.model}")
            if literal_value(session.effort) is not None:
                continue
            keys.append(
                f"efforts.{task.model}.{session.effort}"
                if (not literal_model
                    and session.effort in settings.tier_efforts.get(task.model, {}))
                else f"efforts.{session.effort}")
        used = ", ".join(
            f"{key} = {cfg.source_of(f'adapter.{adapter_name}.{key}')}"
            for key in dict.fromkeys(keys))
        lines.append(f"  {adapter_name}: {used}")
    return lines


def _setting_source_label(cfg: Config, key: str) -> str:
    """Render both the winning layer and whether it is an implicit fallback."""
    layer = cfg.source_of(key)
    if layer == "builtin":
        return "builtin (built-in fallback)"
    return f"{layer} (explicit settings layer)"


def _review_effort_source_key(cfg: Config, review) -> str:
    settings = cfg.adapter_settings(review.adapter)
    tier = settings.tier_efforts.get(review.model, {})
    if review.effort in tier:
        return f"adapter.{review.adapter}.efforts.{review.model}.{review.effort}"
    if review.effort in settings.efforts:
        return f"adapter.{review.adapter}.efforts.{review.effort}"
    return "adapter.%s.efforts.%s" % (review.adapter, review.effort)


def _auto_fix_review_source_lines(cfg: Config) -> list[str]:
    """Show the configured workflow roles and fully resolved session identities."""
    steps = tuple(
        step for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep))
    if not steps:
        return ["Auto-fix workflow: no plan review step configured"]
    lines = [
        "Auto-fix workflow plan (configured order; source: "
        f"{_setting_source_label(cfg, 'workflow.plan')}):"]
    for index, step in enumerate(steps):
        adapters = " -> ".join(step.adapters)
        if not step.produces_verdict:
            action = (f"bounded repair via {adapters}"
                      if step.writes else "no review session")
            lines.append(f"  step {index}: role={step.role}; {action}")
            continue
        lines.append(
            f"  step {index}: role={step.role}; {adapters} / "
            f"{step.model}->{step.requested_model} / "
            f"{step.effort}->{step.requested_effort}")
    return lines


def check(cfg: Config) -> int:
    if not has_git_marker(cfg.root):
        print(GIT_REQUIRED_MESSAGE)
        return 1

    for line in _config_source_lines(cfg):
        print(line)
    for line in _auto_fix_review_source_lines(cfg):
        print(line)

    # The same contracts `run` refuses to start a session without.
    ok, contract_lines = _contract_lines()
    for line in contract_lines:
        print(line)

    # Parsing proves the task-file schema, including non-empty scope declarations.
    # Whether those declarations cover every semantic owner remains a planning review.
    plan: Plan | None = None
    try:
        plan = Plan.parse(cfg.tasks_dir)
        print(
            f"Task-file format: OK ({len(plan.tasks)} tasks; scope declarations "
            "syntactically valid and non-empty; dependencies acyclic). Semantic "
            "scope completeness remains a planning-review decision.")
        try:
            blocks = resolve_task_assignments(cfg, plan)
            print_task_assignments(blocks)
            for line in _assignment_source_lines(cfg, blocks):
                print(line)
        except AssentError as e:
            ok = False
            print(f"Task assignment: FAIL ({e})")
    except AssentError as e:
        ok = False
        print(f"Task-file format: FAIL ({e})")

    # Dependency declaration format and reference integrity of the selected folder; the
    # whole-graph cycle check is validated by the no-argument CLI check.
    try:
        dependencies = parse_folder_dependencies(cfg.tasks_dir)
        after = ", ".join(dependencies.after) or "none"
        print(f"Folder dependencies: OK (after = {after})")
    except AssentError as e:
        ok = False
        print(f"Folder dependencies: FAIL ({e})")

    # adapter resolves
    adapter: Adapter | None = None
    try:
        adapter = get_adapter(cfg.adapter_name, cfg)
        print(f"adapter: OK ({cfg.adapter_name})")
    except AssentError as e:
        ok = False
        print(f"adapter: FAIL ({e})")

    # Every model/effort the plan could still send, proven against the active adapter's
    # capability contract; the same gate `run` applies before opening a session.
    if adapter is not None and plan is not None:
        errors = capability_errors(cfg, adapter, plan)
        if errors:
            ok = False
            print(f"{cfg.adapter_name} capability preflight: FAIL")
            for message in errors:
                print(f"  - {message}")
        else:
            print(f"{cfg.adapter_name} capability preflight: OK")

    # git repo
    inside = _git_read(cfg.root, "rev-parse", "--is-inside-work-tree")
    if inside == "true":
        print("git repo: OK")
        try:
            errors = worktree_configuration_errors(cfg)
        except AssentError as e:
            errors = [str(e)]
        if errors:
            ok = False
            print("worktree version-control layering: FAIL")
            for error in errors:
                print(f"  - {error}")
        else:
            print("worktree version-control layering: OK")
    else:
        ok = False
        print("git repo: FAIL (project root is not a git working tree, or git is not installed/on PATH)")

    # The current adapter's CLI is executable (the probe is provided by the adapter itself, so
    # this is not hardcoded to only claude/codex; an unresolved adapter has already failed above)
    if adapter is not None:
        label = cfg.adapter_name
        probe_ok, message = adapter.probe_cli()
        if probe_ok:
            print(f"{label} CLI: OK ({message})")
        else:
            ok = False
            print(f"{label} CLI: FAIL ({message})")

    print("Result: passed" if ok else "Result: some items failed")
    return 0 if ok else 1
