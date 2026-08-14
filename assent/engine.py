"""Main loop: select task -> run -> accept -> checkpoint/retry/quota wait.

The pre-session decisions this loop shares with the read-only query commands
live in ``assent.preflight``; the report it refreshes as a best-effort side
effect is rendered by ``assent.inspection``.  Both are imported here and neither
imports back, so the query commands stay loadable without the scheduler.

Iron rules inherited from field experience on the workflow project: **never discard
output the execution AI has already burned tokens to produce.**
- Quota or checkpoint-resume interrupted -> keep progress in a wip checkpoint, then resume with
  a "continue" prompt instead of scrapping and rerunning.
- Acceptance failed -> do not restore the working tree; a retry fixes the existing work
  (cheap) rather than redoing it from scratch (expensive).
- Retries exhausted -> commit the not-yet-passing work into a BLOCKED checkpoint and hand
  the final call to a human.
- scope check is fail-closed: parsing a task file forces scope to be non-empty, so the
  batch parse at the start of a run is a zero-token refuse-to-run gate.

Acceptance defences added by assent (the format contract's "defence rules"):
- The only legal change the execution AI may make to its own task file is the status line;
  at acceptance time the checkpoint version and the on-disk version are compared field by
  field, and any other tampered field (loosened scope, swapped verify, changed deps) fails
  acceptance.
- scope/verify are always taken from the checkpoint version of the task file, never from
  the on-disk version.
- A task's own t file and r file are auto-excluded from scope (the status update and the
  journal append are part of the job).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Callable, TextIO

from assent import (AssentError, auto_fix, contracts, gitops, lockfile,
                    reconcile, rework, shared_paths, usage, verification)
from assent.adapters import Adapter, InvocationRequest, get_adapter
from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     run_subprocess as _adapter_run_subprocess,
                                     stop_wake_requested)
from assent.config import (Config, WorkflowActionStep, WorkflowPlanStep,
                           WorkflowTaskStep, load_config,
                           validate_task_workflow_steps)
from assent.batch_verification import (SelectionCandidateConflict,
                                       selection_conflict_line,
                                       selection_conflicts_from_evidence,
                                       verify_selected_batch_action)
from assent.folderdeps import (find_unfinished_prerequisites,
                               order_folders_by_dependency,
                               parse_folder_dependency_graph)
from assent.folder_verification_closeout import verify_folder_action
from assent.inspection import try_write_report
from assent.modeling import (effort_identity, has_literal, inherited_effort,
                             literal_value)
from assent.plan import (Plan, Task, TaskWorkflowAction, append_entry,
                         parse_task_file,
                         read_entries, read_selection_workflow_state,
                         read_workflow_state, same_except_status,
                         set_status, WorkflowState,
                         SelectionWorkflowState,
                         add_scope_entries, scope_text_without_entries,
                         scope_text_with_entries, task_text_sha256,
                         workflow_state_path, write_selection_workflow_state,
                         write_workflow_state)
from assent.preflight import (GIT_REQUIRED_MESSAGE, SessionIdentity,
                              StackState, auto_fix_fixer_capability_errors,
                              auto_fix_review_capability_errors,
                              has_git_marker, literal_adapter_errors,
                              resolve_session, resolve_stack_state,
                              worktree_configuration_errors)
from assent.verification_common import (sha256_file, source_snapshot,
                                        summary as verification_summary)


def _invoke_adapter(
        cfg: Config, adapter: Adapter, adapter_name: str, prompt: str,
        requested_model: str, requested_effort: str | None, cwd, *,
        context_kind: str, context_id: str,
        folders: tuple[str, ...] | None = None, structured: bool = False):
    """Run one provider command and record its result without making usage gating."""
    invocation_id = usage.new_invocation_id()
    method = adapter.run_structured_task if structured else adapter.run_task
    result = method(prompt, requested_model, requested_effort, cwd)
    try:
        usage.record_invocation(
            cfg.assent_dir, invocation_id=invocation_id, adapter=adapter_name,
            requested_model=requested_model, context_kind=context_kind,
            context_id=context_id, folders=folders or (cfg.tasks_name,),
            evidence=result.usage)
    except Exception:
        # Telemetry is derived observability evidence and can never replace the
        # provider result with a workflow failure.
        pass
    return result

# The worker session's opening prompt (variables are substituted literally,
# tolerating other braces inside the template).
_PROMPT_TEMPLATE = (
    "You are the assent execution AI. First read the project rules {agents_md_path},\n"
    "then read the assent working instructions {instructions_path} and the task file {task_path}.\n"
    "Run only task {task_id}; do not touch other task files.\n"
    "This run's journal identity is by = \"{agent}\", requested_model = \"{requested_model}\".\n"
    "That resolved identity is authoritative for this run's journal entry, even when the\n"
    "working instructions or the existing entries only show other agent names.\n"
    "requested_model is the --model value passed to the AI CLI this run.\n"
    "This run's selected effort = \"{effort}\", actual requested_effort = \"{requested_effort}\";\n"
    "requested_effort is the value actually passed to the AI CLI this run; "
    "<vendor-default> means no effort argument was passed and the journal must "
    "omit requested_effort.\n"
    "{focused_test_policy}\n"
    "When done:\n"
    "1. Change the status of {task_path} to DONE or BLOCKED -- the status line is the only\n"
    "   line you may change in the whole task file.\n"
    "2. Append one [[entry]] journal record to the end of {journal_path} (TOML, with time,\n"
    "   by = \"{agent}\", requested_model = \"{requested_model}\", requested_effort, event,\n"
    "   summary, detail; create the file if it does not exist).\n"
    "Do not run git commit; the scheduler owns the checkpoint."
)
_RETRY_SUFFIX = ("\nThe previous attempt failed acceptance. Reason: {failure_reason}. "
                 "The previous attempt's work is still in the working tree; review and fix "
                 "it on top of what is there, do not redo it.")
_RESUME_SUFFIX = ("\nThe previous adapter session was interrupted (quota exhausted, "
                  "checkpoint-resume control, or user "
                  "interrupt); the partial work already done is kept in the working tree "
                  "(possibly including a wip checkpoint). Review the current state first, "
                  "resume and finish the remaining part, and do not redo what is already done.")
_TASK_WORKFLOW_SUFFIX = """

Task workflow step {position} of {total}; scheduled role: {role}

Role abilities:
{role_policy}

{closeout_policy}
"""
_TASK_VERDICT_POLICY = """

This role produces the decision for the task-layer failure supplied below:
either a failed focused_test or durable evidence that an earlier task role
self-marked BLOCKED. Finish with exactly one assent.auto_fix_review JSON object
on the last non-empty output line. A finding must use the existing review
schema and name this task. PASS is invalid while a focused_test is failing;
for a prior BLOCKED result, PASS is valid only when no repair is required.

{write_policy}

For an exact omitted task-scope file, use kind "scope_amendment", make path and
scope_addition.path the same normalized project-relative path, and set
scope_addition.path_state to "existing_file" or "new_file" according to the
state at the start of this session. Never edit the task contract yourself; the
scheduler validates and appends an accepted exact path at closeout.
"""
_PLAN_WORKER_PROMPT = """You are the Assent plan execution worker.

First read the project rules {agents_md_path} and the Assent working instructions
{instructions_path}. This folder uses `[workflow].task = []`, so the whole plan,
not any individual task, is the accountability unit. Do not edit any task file or
journal; the scheduler owns their closeout.

Plan workflow step {position} of {total}; scheduled role: {role}

Role abilities:
{role_policy}

You may write only within this union of every task's declared scope:
{scope}

The scheduler will run these focused commands, deduplicated in plan order:
{verify_commands}

Task contracts:
{contracts}

Folder dependency state:
{dependency_state}
{completed_context}
Do not run the full project verifier and do not commit. Complete the scheduled
role and return normally; the scheduler decides the gate, journals, statuses,
retry, and checkpoint.
"""
_REWORK_SUFFIX = ("\nThe previous implementation of this task was rejected by a human "
                  "reviewer, and its status was reset to TODO.{reject_reason_clause}\n"
                  "The previous implementation and its tests may still be present in the "
                  "working tree; do not assume the existing code or existing tests are "
                  "correct as-is, re-evaluate their correctness from scratch. If the task "
                  "file has since been amended with new clauses, the task file is "
                  "authoritative.")
_CLOSEOUT_ONLY_MARKER = "verify passed; only closeout missing"
_CLOSEOUT_ONLY_REASON_TEMPLATE = (
    _CLOSEOUT_ONLY_MARKER + " -- status not updated to DONE/BLOCKED (currently "
    "{status}); focused verify already passed: {verify_command}")
_CLOSEOUT_RETRY_SUFFIX = (
    "\nThe previous attempt failed acceptance only because the task status was not "
    "updated. The implementation in the working tree is already complete, and this "
    "task's focused verify command has already passed: {verify_command}\n"
    "This session must only close out the task: change the status line in the task "
    "file to DONE, and append one [[entry]] journal record to the journal file. "
    "Do not modify any code or tests.")

_AUTO_FIX_REVIEW_PROMPT = """You are the Assent plan reviewer.

Before inspecting source, read the project rules {agents_md_path} and the Assent
session rules {instructions_path}. Read other project files only as needed for
this bounded review.

Review context: {review_context}
Review stage: {review_stage}

Scheduled workflow role: {workflow_role}
Role abilities:
{role_policy}

{write_policy}

{round_policy}

Complete verification has deliberately not run yet. The absence of a full
suite result, integration candidate, verification receipt, or any other
complete-verification evidence is never a finding. Do not run or request
complete verification.

{context_policy}

{stage_policy}

Review all of the following before deciding:
- the scheduler-supplied cumulative checkpoint diff
- every authoritative checkpoint task contract and relevant journal below
- any current on-disk task text explicitly labelled UNTRUSTED evidence
- the exact scheduler blocker reasons and focused-command evidence
- the remaining folder dependency state
- implementation and tests named by those contracts, plus directly necessary
  interacting code encountered through read-only inspection

Report only blocking correctness, safety, unmet-requirement, or focused-test-gap
findings allowed by the context and stage policies. A focused-test-gap finding
must identify one concrete task requirement that lacks a local, reliably
runnable focused regression test. Every finding must cite an existing task
requirement or concrete repair regression; never invent a new acceptance
criterion. Speculation, uncertainty without evidence,
idealized design, style, preference, optional improvement, and unrelated scope
expansion cannot block PASS.

Finish with exactly one JSON object on the last non-empty output line and no
later text. PASS example:
{{"type": "assent.auto_fix_review", "verdict": "PASS", "shared_paths": null, "findings": []}}
`shared_paths` must be null unless the shared-path clause below requires a
decision object.
Every finding of a non-PASS verdict must supply all schema fields: kind,
task_id, path, summary, evidence, recommendation, scope_addition, transition,
prior_fingerprint, and transition_evidence. Use null where an optional field is
absent.

When the blocker is exactly an omitted task scope path, use kind
"scope_amendment", name the existing task_id, and make path and
scope_addition.path the same normalized exact project-relative file path.
scope_addition.path_state must be "existing_file" for an existing ordinary
file or "new_file" for an absent leaf below an existing ordinary directory.
Never propose a glob, directory, control/management path, removal, verifier
change, new task, or unrelated scope expansion.
{scope_policy}

Folder: {folder}
Source tree: {source_tree}
Base commit: {base_ref}

Scheduler-supplied cumulative checkpoint diff:
{cumulative_diff}

Exact durable blocker evidence:
{blocker_evidence}

Scheduler-supplied focused evidence:
{focused_evidence}

Remaining folder dependency state:
{dependency_state}

Prior review and repair evidence:
{prior_evidence}

Task contracts and journals:
{management_evidence}
"""

_AUTO_FIX_REVIEW_CORRECTION_EXAMPLE = (
    '{"type":"assent.auto_fix_review","verdict":"FAIL","findings":['
    '{"kind":"scope_amendment","task_id":"t001",'
    '"path":"src/example.py","summary":"Required file is outside scope",'
    '"evidence":"The task requires this exact file.",'
    '"recommendation":"Add this exact path to the task scope.",'
    '"scope_addition":{"path":"src/example.py",'
    '"path_state":"existing_file"},"transition":"initial",'
    '"prior_fingerprint":null,"transition_evidence":null}]}')


def _auto_fix_review_correction(error: str, diagnostic: str) -> str:
    """Tell the same reviewer exactly how its rejected terminal record failed."""
    return (
        "\n\nREVIEW OUTPUT CORRECTION REQUIRED\n"
        "The exact review-record validator rejected the previous output:\n"
        f"{error}\n"
        "Bounded diagnostic of the rejected output:\n"
        f"{diagnostic}\n"
        "Return a corrected record without inferring, moving, or omitting fields. "
        "Here is one schema-complete non-PASS JSON example; optional values "
        "that are absent are null:\n"
        f"{_AUTO_FIX_REVIEW_CORRECTION_EXAMPLE}")

_AUTO_FIX_READ_ONLY_POLICY = """This is a blocking decision gate, never an
implementation session. Do not edit, create, delete, rename, format, or
otherwise write any project or management-plane file. Do not run tests,
formatters, generators, or any other command that may write. Perform read-only
inspection and rely on the exact scheduler-supplied focused evidence below.
This cooperative write check does not make the worktree a security sandbox and
cannot intercept effects outside the project."""

_AUTO_FIX_MERGED_WRITE_POLICY = """This is a merged reviewer-fixer round: when
you find a genuine blocking problem you may repair it directly instead of only
reporting it. You may write inside the declared scope of the one existing task
your finding names. If exactly one required file was omitted from that scope,
repair it now and return the exact scope_amendment with verdict FIXED; the
scheduler validates and appends that path at closeout. Every other write is refused by the same structural
safety gate an ordinary worker session faces -- a management-plane file, a task
file, another task's scope, a commit, or any write in the primary worktree
makes your verdict unusable while preserving your exact edits. Assent alone
owns task status, task contracts, and Git state: never create a task, change a
task's requirements or scope, revert or delete sources, or accept the folder.

Report a repair with verdict "FIXED", carrying the same finding fields a
reported blocker uses: what was wrong in summary and evidence, and what you
changed in recommendation. Return "PASS" only when nothing blocking remains
and you wrote nothing at all."""

_AUTO_FIX_SCOPE_WRITE_POLICY = """Repair that exact omitted path in this same
session and return FIXED. The scheduler validates the pre-session path state,
the exact addition, and all session writes, then persists the task-contract
amendment at closeout."""

_AUTO_FIX_SCOPE_READ_ONLY_POLICY = """Return FAIL with the exact scope
amendment. This read-only role cannot repair the omitted path."""

_AUTO_FIX_ROUND_POLICY = """This is review round {position} of {total}.
Review rounds remaining after this one: {remaining}. Converge within the
configured finite rounds."""

# Blocked adjudication is a separate read-only entry point, not one of the
# merged reviewer-fixer rounds, so it must never receive the round sequence's
# final-round instruction to repair the blocker itself -- that would contradict
# the read-only write policy in the very same prompt.
_AUTO_FIX_BLOCKED_ROUND_POLICY = """This adjudication is a single read-only
decision gate, not one of the merged reviewer-fixer rounds. Report what you
find; repairing it yourself is not available in this context."""

_AUTO_FIX_FINAL_ROUND_POLICY = """This is the FINAL review round.
No further automated review will occur after this one. Return PASS only when
the existing requirements pass, FIXED only when you actually repaired the
blocker, or FAIL with the concrete unresolved evidence. Never invent an
acceptance criterion and never pretend a blocker was fixed."""

_AUTO_FIX_COMPLETED_POLICY = """This COMPLETED_FOLDER review examines the
bounded cumulative implementation. Only COMPLETED_FOLDER + INITIAL may report
eligible technical debt: it must be concrete, encountered in changed or
directly interacting code, local to an existing task's declared scope, and
reliably testable by that task's focused gate. Do not conduct a repository-wide
debt search."""

_AUTO_FIX_BLOCKED_POLICY = """This BLOCKED_ADJUDICATION reviews only the durable
blockers, their trusted requirements, and directly necessary interacting code.
Unfinished tasks, TODO work, and the incomplete folder state are expected and
cannot be findings. Technical debt is not eligible in this context. A
self-marked BLOCKED task legitimately has no focused result because ordinary
closeout skips that gate; that absence is not a finding.

One incomplete state is the exception, because it is the only one a repair can
end: a task whose scheduler evidence records its focused verify command as
PASS and whose sole remaining blocker is the unwritten status. Report it with
kind = "blocked_recovery", summary and evidence quoting that recorded PASS and
the status the task still carries, and a recommendation to re-run that focused
command and write the status, so the repair session can finish the closeout.
Such a blocker owns no file, but every finding must name a path inside the
named task's declared scope: name any one of that task's scope entries. You may
not reach this verdict from the code looking finished -- only the scheduler's
own recorded focused PASS establishes it."""

_AUTO_FIX_INITIAL_POLICY = """This INITIAL review has no prior reviewer finding
to resolve. Findings must use transition = \"initial\" and must not cite a prior
fingerprint."""

_AUTO_FIX_RECHECK_POLICY = """This RECHECK must decide the prior current
findings first. Omit resolved findings. Reproduce a still-present finding
without semantic rewording, using transition = \"still_present\" and its exact
scheduler prior_fingerprint. A new blocker is allowed only as a concrete repair
regression (transition = \"repair_regression\") or a newly exposed violation of
an existing task requirement (transition = \"newly_exposed\"), with concrete
transition_evidence. For repair_regression, that evidence must name the exact
path changed by the repair. For newly_exposed, it must name the existing task id
and whether the origin is its goal, behavior, acceptance, or other requirement.
Repeated technical-debt discovery is forbidden. When no
prior blocker remains and no qualifying new blocker is evidenced, the evidence
is sufficient and you must immediately PASS rather than continue searching for
improvements."""

_AUTO_FIX_REPAIR_SUFFIX = """

This is an Assent-authorized bounded auto-fix attempt. Preserve the current
implementation and repair the findings owned by this task; do not create tasks,
change task requirements or scope, revert code, accept work, or delete sources.
The exact durable repair brief is reproduced below. Re-evaluate current code
rather than assuming a finding is still present, and run this task's ordinary
focused gate before closeout.

Durable repair brief (preserved verbatim across restart):
{repair_brief}

Your closeout journal detail must contain exactly one line for every fingerprint
listed under Current findings, with no unknown or duplicate fingerprint:
ASSENT_REPAIR_DISPOSITION {{"fingerprint":"<scheduler fingerprint>","disposition":"fixed|not_reproducible|still_blocked","detail":"<nonempty concrete evidence or reason>"}}
Use only those three JSON string fields. DONE permits fixed or
not_reproducible; still_blocked requires BLOCKED. These acknowledgement lines
do not replace any structural, scope, focused, or independent-review gate.
"""

_SELECTION_VERIFICATION_REVIEW_PROMPT = """You are the Assent selection verifier reviewer.

Before inspecting source, read the applicable project rules:
{agents_md_paths}
Then read the Assent session rules {instructions_path}. Read other project
files only as needed for this bounded review.

Review context: SELECTION_VERIFICATION
Scheduled workflow role: {workflow_role}
Role abilities:
{role_policy}

{write_policy}

Candidate construction or the complete verifier has failed against the exact
selection identified below; for typed candidate conflicts the verifier
deliberately did not run. Diagnose only that bounded failure and return the
existing Assent review record. Do not create a task, change task status or
requirements, accept a source, shrink the selection, or propose unrelated work.

Every finding must name one existing task id and a normalized project-relative
path owned by that task's declared scope. When the exact blocker is an omitted
scope path, use kind "scope_amendment" and the existing exact scope-addition
contract. Task ids may repeat between folders; the task id plus path must
resolve to exactly one selected plan, or the scheduler will retain the evidence
for a human decision.

Finding kind describes the defect: use "correctness", "safety",
"unmet_requirement", or "focused_test_gap" (or "scope_amendment" only for the
exact omission above). "target_alone" and "peer_only" describe conflict origin
in the supplied evidence; they are not finding kinds.

For typed candidate-conflict evidence, return one finding for every exact
folder/path pair in the complete wave and no unrelated finding. Preserve the
folder ownership implied by task id plus scope. A target-alone finding will be
edited in Assent's managed reconcile worktree; a peer-only finding will reopen
the existing owning task. Never recommend accepting or omitting a prefix.

Finish with exactly one JSON object on the last non-empty output line and no
later text. PASS is invalid because the supplied verifier result failed.
{verdict_policy}

Scheduler-owned repair workspaces:
{repair_workspaces}

Selection and verifier evidence:
{selection_evidence}

Focused evidence:
{focused_evidence}

Cumulative source diffs:
{cumulative_diffs}

Prior selection-review evidence:
{prior_evidence}

Authoritative task contracts and journals:
{management_evidence}
"""

_SELECTION_RECONCILE_PROMPT = """You are the Assent selection conflict fixer.

Before editing, read the project rules {agents_md_path} and the Assent session
rules {instructions_path}. Read other project files only as needed to resolve
the listed conflicts.

Scheduled workflow role: {workflow_role}
Role abilities:
{role_policy}

The user authorized automatic conflict repair for one exact selection. Assent
has prepared a source-first reconcile merge in the worktree below. Edit only
the exact conflict paths listed here. Do not stage or commit, run Git or Assent,
change refs or task files, run tests or the full verifier, or edit any other
path. Assent owns validation, staging, the merge commit, source fast-forward,
focused gates, candidate rebuild, and cleanup.

Folder: {folder}
Worktree: {worktree}
Source tip (ours): {source_tip}
Target tip (theirs): {target_tip}
Exact conflict paths:
{conflict_paths}

Scheduler-captured three-way conflict evidence:
{three_way_evidence}

Authoritative task contracts for this folder:
{contracts}

Resolve every listed conflict semantically and return normally. Leave the
files unstaged so the scheduler can prove the conflict scene before continuing.
"""

_AUTO_FIX_EVIDENCE_LIMIT = 64_000
# The prior-review section is lineage bookkeeping rendered from the durable
# state a new verdict then replaces, so a recheck's own prompt can never be
# rebuilt byte for byte once its result is written.  Hashing that section into
# the reuse identity would therefore make every recheck PASS unusable on the
# next unchanged invocation; the identity hashes this placeholder instead and
# still covers the template, both policies, the stage and context, the diffs,
# the focused evidence, and every task contract and journal.
_AUTO_FIX_PRIOR_EVIDENCE_IDENTITY = (
    "(review lineage; excluded from the reusable review identity)")

_QUOTA_BUFFER = timedelta(minutes=2)  # reset time + buffer, to avoid being blocked again right at the edge
_QUOTA_TICK = 1.0                     # countdown refresh interval (seconds)
# Longest single sleep the non-tty countdown may take. A quota wait is often
# hours long, and a lone multi-hour sleep is what made a stop request invisible:
# on POSIX _thread.interrupt_main() only sets a pending exception that is
# delivered when bytecode next runs, so the wait had to finish first. Splitting
# it bounds that delivery delay without changing the total wait.
_COUNTDOWN_SEGMENT = 60.0
_ADAPTER_DIAGNOSTIC_LIMIT = 240
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)([\"']?(?:api[_ -]?key|access[_ -]?token|token|password|secret|"
    r"authorization)[\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)")
_BEARER_RE = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk(?:-ant)?|ghp|github_pat)-[A-Za-z0-9_-]{8,}\b")
_REWORK_REASON_RE = re.compile(
    r"(?:^|\n)reason: (.*?)(?=\nHEAD before operation:|\Z)", re.DOTALL)


@dataclass
class _SessionState:
    """Lets the outer interrupt handler read the current session's closeout state."""

    identity: SessionIdentity | None = None
    terminal_checkpoint: bool = False
    terminal_checkpoint_attempt: tuple[str, str] | None = None


@dataclass
class _ActiveTask:
    """Mutable interrupt witness shared by normal and auto-fix task loops."""

    task: Task | None = None
    session: _SessionState | None = None


@dataclass(frozen=True)
class _FixerProfile:
    """One reopened task's repair identity for a single round.

    The merged reviewer-fixer loop terminates by walking the configured review
    round sequence, so this identity is derived per round and never persisted.
    """

    adapters: tuple[str, ...]
    model: str
    effort: str | None

    @property
    def adapter(self) -> str:
        return self.adapters[0]


@dataclass(frozen=True)
class _AutoFixReviewOutcome:
    """One final-focused/reviewer cycle and its durable repair evidence."""

    code: int
    state: auto_fix.AutoFixState | None = None
    human_reason: str | None = None
    # The configured review-round sequence was walked to its end without an
    # independent PASS.  The finite loop stops here; how that terminal state is
    # reported to a human is the following task's decision.
    rounds_exhausted: bool = False


@dataclass(frozen=True)
class _AutoFixBlockerEvidence:
    """One terminal task failure supplied to blocked adjudication."""

    task: Task
    trigger: str
    reason: str
    focused_evidence: str
    worker_summary: str | None = None


@dataclass(frozen=True)
class _FocusedGateIdentity:
    """The exact state one scheduler-owned focused PASS was proven against."""

    command: str
    source_tree: str
    shared_inputs: str


class _FocusedGateLedger:
    """Scheduler focused PASSes reusable inside one invocation and repair round.

    The scheduler stays the only authority: an entry is written after its own
    focused command exited 0 and the task reached its terminal checkpoint, and
    it is bound to that command, the resulting checkpoint tree, the current
    shared-input digest, and a clean worktree.  A later task or repair that
    writes source moves the tree and therefore invalidates every earlier entry
    mechanically, without consulting a status, timestamp, or AI claim.

    Evidence lives in memory only -- deliberately no receipt, state file, task
    field, or cross-restart cache -- so a restart re-runs the command instead of
    reconstructing a PASS it cannot prove.
    """

    def __init__(self) -> None:
        self._passes: set[_FocusedGateIdentity] = set()

    def record(self, cfg: Config, command: str) -> None:
        """Retain one just-passed focused command bound to the current state."""
        identity = self._identity(cfg, command)
        if identity is not None:
            self._passes.add(identity)

    def reusable(self, cfg: Config, command: str) -> bool:
        """Return whether an earlier PASS still matches the current state exactly."""
        identity = self._identity(cfg, command)
        return identity is not None and identity in self._passes

    @staticmethod
    def _identity(cfg: Config, command: str) -> _FocusedGateIdentity | None:
        """Bind a command to the current checkpoint tree and shared inputs.

        Any state that cannot be proven right now -- a dirty worktree, an
        unreadable checkpoint, an unsettled or disagreeing shared-path contract
        -- yields no identity at all, so nothing is retained and nothing is
        reused: the caller simply runs the command.
        """
        try:
            if not gitops.working_tree_status(
                    cfg.root, cfg.git_excludes).is_clean:
                return None
            source_tree = gitops.tree_of(cfg.root, "HEAD")
            contract = _shared_paths_contract(cfg)
            if not contract.settled:
                return None
            shared_inputs = shared_paths.shared_inputs_digest(
                gitops.main_worktree(cfg.root), [(cfg.tasks_name, contract)])
        except (AssentError, OSError):
            return None
        return _FocusedGateIdentity(command, source_tree, shared_inputs)


class _AdapterProcessCreationError(OSError):
    """The shared runner proved its subprocess constructor did not return."""


def _adapter_process_creation_failed(error: OSError) -> bool:
    """Return whether the shared runner proves no child handle was created.

    An arbitrary adapter ``OSError`` is not a start witness: collection, wait,
    and cleanup can also fail after a child exists.  In the shared runner, the
    local ``proc`` is assigned by the sole ``Popen`` call.  Its exact traceback
    frame without that local therefore identifies only the constructor-failure
    boundary, without trusting exception wording or an adapter assertion.
    """
    traceback = error.__traceback__
    target = _adapter_run_subprocess.__code__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code is target:
            return "proc" not in frame.f_locals
        traceback = traceback.tb_next
    return False


@dataclass
class _AdapterRotation:
    """Run-scoped adapter cursor and quota evidence shared across tasks."""

    names: tuple[str, ...]
    adapters: tuple[Adapter, ...]
    index: int = 0
    exhausted: set[str] = field(default_factory=set)
    auth_failed: set[str] = field(default_factory=set)
    pool: dict[str, Adapter] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.pool:
            self.pool = dict(zip(self.names, self.adapters))

    @property
    def name(self) -> str:
        return self.names[self.index]

    @property
    def adapter(self) -> Adapter:
        return self.adapters[self.index]

    def session_opened(self) -> None:
        """A non-quota result proves the rotation is no longer fully exhausted."""
        self.exhausted.clear()

    def advance_after_quota(self, failed: set[str] | None = None) -> bool:
        """Move to the next adapter; return whether this exhausted one complete cycle."""
        self.exhausted.add(self.name)
        unavailable = self.exhausted | self.auth_failed | (failed or set())
        recoverable = [index for index, name in enumerate(self.names)
                       if name not in self.auth_failed]
        if not recoverable:
            raise AssentError("Adapter rotation has no authenticated candidate")
        cycle_exhausted = all(
            self.names[index] in unavailable for index in recoverable)
        if cycle_exhausted:
            preferred = next(
                (index for index in recoverable
                 if self.names[index] in self.exhausted), recoverable[0])
            self.exhausted.clear()
            if failed is not None:
                failed.clear()
            self.index = preferred
            return True
        for offset in range(1, len(self.names) + 1):
            index = (self.index + offset) % len(self.names)
            if self.names[index] not in unavailable:
                self.index = index
                break
        return cycle_exhausted

    def subset(self, names: tuple[str, ...]) -> "_AdapterRotation":
        """Return a fresh step-local cursor backed by the preflighted adapters."""
        try:
            adapters = tuple(self.pool[name] for name in names)
        except KeyError as error:
            raise AssentError(
                f"Task workflow adapter {error.args[0]!r} was not preflighted") from error
        return _AdapterRotation(names, adapters, pool=self.pool)

    def advance_after_failure(self, failed: set[str]) -> bool:
        """Try the next not-yet-failed candidate in this step-local cursor."""
        failed.add(self.name)
        unavailable = failed | self.exhausted | self.auth_failed
        for offset in range(1, len(self.names)):
            index = (self.index + offset) % len(self.names)
            if self.names[index] not in unavailable:
                self.index = index
                return True
        recoverable = [index for index, name in enumerate(self.names)
                       if name not in self.auth_failed]
        failed.clear()
        self.exhausted.clear()
        self.index = recoverable[0]
        return False

    def advance_after_authentication(self, failed: set[str]) -> str:
        """Skip one unauthenticated candidate; switch, wait, or require login."""
        self.auth_failed.add(self.name)
        unavailable = self.auth_failed | self.exhausted | failed
        for offset in range(1, len(self.names)):
            index = (self.index + offset) % len(self.names)
            if self.names[index] not in unavailable:
                self.index = index
                return "switch"
        recoverable = [index for index, name in enumerate(self.names)
                       if name not in self.auth_failed]
        if not recoverable:
            return "required"
        preferred = next(
            (index for index in recoverable
             if self.names[index] in self.exhausted), recoverable[0])
        failed.clear()
        self.exhausted.clear()
        self.index = preferred
        return "wait"


class _BillingAbort(Exception):
    """An account-level billing/insufficient-balance failure the adapter classified.

    Unlike quota (a rate-limit window that resets on its own), a prepaid balance does not
    refill, so retrying is provably futile and the next TODO task would hit the identical
    failure.  This unwinds the whole run to the abort handler, which keeps progress in a wip
    checkpoint and leaves the task unresolved for a clean resume after a manual top-up.  It is
    dispatched purely on ``TaskResult.failure_kind == "billing"`` -- never on an adapter name --
    so a future adapter gets the behaviour for free by setting the same string.
    """


class _AuthenticationRequired(AssentError):
    """Every declared adapter for one workflow step requires human login."""


def _authentication_required_message(rotation: _AdapterRotation,
                                     reason: str) -> str:
    names = ", ".join(rotation.names)
    return ("AUTHENTICATION REQUIRED: every declared adapter requires login "
            f"({names}). Sign in, then rerun to resume. Last failure: {reason}")


def _authentication_failover_action(rotation: _AdapterRotation,
                                    failed: set[str], reason: str) -> str:
    action = rotation.advance_after_authentication(failed)
    if action == "required":
        raise _AuthenticationRequired(
            _authentication_required_message(rotation, reason))
    return action


def _workflow_step_rotation(
        cfg: Config, step: WorkflowPlanStep,
        injected: Adapter | None = None) -> _AdapterRotation:
    """Resolve one plan/integration role's ordered adapter candidates."""
    adapters = tuple(
        injected if index == 0 and injected is not None else get_adapter(name, cfg)
        for index, name in enumerate(step.adapters))
    return _AdapterRotation(step.adapters, adapters)


def _adapter_availability_failed(result: TaskResult) -> bool:
    """Return whether a failed provider session may use a declared fallback."""
    return ((result.exit_code != 0 or result.stalled)
            and result.failure_kind not in {
                "authentication", "billing", "interrupt", "permission",
                "unsupported_model"})


# --------------------------------------------------------------------------- #
# Prompt / small helpers
# --------------------------------------------------------------------------- #
def _diagnosed_shared_inputs(cfg: Config) -> tuple[str, ...]:
    """Directories a stored full-verifier diagnosis proved this folder needs.

    No generic rule can infer that an ignored directory is semantically
    required, so a complete verifier that already failed on one is the evidence
    that invalidates a profile which does not declare it.  A missing or
    unreadable receipt simply contributes nothing.
    """
    try:
        receipt = verification.read_verification_receipt(
            verification.receipt_path(cfg), gitops.main_worktree(cfg.root))
    except AssentError:
        return ()
    return verification.diagnosed_ignored_directories(receipt.failure_summary)


def _shared_paths_contract(cfg: Config) -> "shared_paths.Contract":
    """Return this source worktree's current usable shared-path contract.

    Both the bounded review clause and the closeout gate ask the same question of
    the same snapshot. An undeclared link already proven to target the ordinary
    same-relative primary directory becomes bounded STALE review evidence;
    unreadable, ambiguous, foreign, or otherwise unsafe link state still
    propagates as a refusal.
    """
    main = gitops.main_worktree(cfg.root)
    contract = shared_paths.classify(main, cfg.root)
    contract = shared_paths.review_contract_with_source_links(
        main, cfg.root, contract)
    if contract.settled:
        shared_paths.require_directory_link_agreement(main, cfg.root, contract)
    return contract


_PLAN_FOCUSED_SWEEP_PREFIX = "FOCUSED SWEEP ACTION STATE: "
_TASK_FOCUSED_TEST_PREFIX = "FOCUSED TEST ACTION STATE: "
_TASK_REVIEW_PREFIX = "TASK VERDICT STATE: "
_TASK_BLOCKER_PREFIX = "TASK BLOCKER STATE: "
_TASK_SCOPE_AMENDMENT_PREFIX = "TASK SCOPE AMENDMENT: "


@dataclass(frozen=True)
class _TestActionEvidence:
    status: str
    identity: str
    exit_code: int
    command: str
    summary: str


def _focused_sweep_identity(cfg: Config, plan: Plan) -> str:
    """Bind a plan sweep to the source tree and its distinct commands."""
    if not gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        raise AssentError("source worktree is dirty at the focused_sweep boundary")
    commands = _plan_verify_commands(plan)
    command_sha256 = hashlib.sha256(
        json.dumps(commands, ensure_ascii=False, separators=(",", ":"))
        .encode("utf-8")).hexdigest()
    return f"{gitops.tree_of(cfg.root, 'HEAD')}:{command_sha256}"


def _focused_sweep_record(state: WorkflowState) -> _TestActionEvidence | None:
    if state.unit != "plan":
        return None
    encoded = next((item[len(_PLAN_FOCUSED_SWEEP_PREFIX):]
                    for item in reversed(state.focused_evidence)
                    if item.startswith(_PLAN_FOCUSED_SWEEP_PREFIX)), None)
    if encoded is None:
        return None
    try:
        data = json.loads(encoded)
        record = _TestActionEvidence(
            data["status"], data["identity"], data["exit_code"],
            data["command"], data["summary"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AssentError("Plan focused_sweep action evidence is unreadable") from error
    if (record.status not in {"PASSED", "FAILED", "STALE"}
            or not record.identity or not isinstance(record.exit_code, int)
            or not isinstance(record.command, str)
            or not isinstance(record.summary, str)):
        raise AssentError("Plan focused_sweep action evidence has invalid values")
    return record


def _with_focused_sweep_record(
        state: WorkflowState, record: _TestActionEvidence) -> WorkflowState:
    data = json.dumps({
        "status": record.status,
        "identity": record.identity,
        "exit_code": record.exit_code,
        "command": record.command,
        "summary": record.summary,
    }, ensure_ascii=False, separators=(",", ":"))
    ordinary = tuple(
        item for item in state.focused_evidence
        if not item.startswith(_PLAN_FOCUSED_SWEEP_PREFIX))
    return replace(
        state, focused_evidence=ordinary + (_PLAN_FOCUSED_SWEEP_PREFIX + data,))


def _refresh_focused_sweep_evidence(
        cfg: Config, plan: Plan, state: WorkflowState) -> WorkflowState:
    """Turn prior evidence STALE when the source or command union moves."""
    record = _focused_sweep_record(state)
    if record is None or record.status == "STALE":
        return state
    try:
        current = _focused_sweep_identity(cfg, plan)
    except AssentError:
        current = ""
    if current == record.identity:
        return state
    return _with_focused_sweep_record(
        state, replace(record, status="STALE",
                       summary="Source or focused commands changed after focused_sweep."))


def _run_focused_sweep_action(
        cfg: Config, plan: Plan,
        state: WorkflowState) -> tuple[WorkflowState, _TestActionEvidence, bool]:
    """Run or reuse the plan's distinct focused commands."""
    identity = _focused_sweep_identity(cfg, plan)
    existing = _focused_sweep_record(state)
    if (existing is not None and existing.identity == identity
            and existing.status in {"PASSED", "FAILED"}):
        print(f"  focused_sweep {existing.status.lower()} evidence reused "
              f"(exit {existing.exit_code})")
        return state, existing, True

    commands = _plan_verify_commands(plan)
    command = "\n".join(commands)
    armed = _TestActionEvidence(
        "STALE", identity, 0, command,
        "Focused sweep has not completed for this source identity.")
    state = _with_focused_sweep_record(state, armed)
    write_workflow_state(cfg.tasks_dir, state)
    evidence: list[str] = []
    exit_code = 0
    merged = _merged_unittest_passes(cfg, commands)
    for focused_command in commands:
        result = merged.get(focused_command)
        if result is None:
            result = _verify_subprocess(cfg, focused_command)
        _show_verify_result(focused_command, result)
        evidence.append(
            f"{'PASS' if result.returncode == 0 else 'FAIL'}: "
            f"exit {result.returncode}: {focused_command}")
        if result.returncode != 0 and exit_code == 0:
            exit_code = result.returncode
    record = replace(
        armed, status="PASSED" if exit_code == 0 else "FAILED",
        exit_code=exit_code, summary="\n".join(evidence))
    try:
        unchanged = _focused_sweep_identity(cfg, plan) == identity
    except AssentError:
        unchanged = False
    if not unchanged:
        record = replace(
            record, status="STALE",
            summary=verification_summary(
                record.summary,
                "Source or focused commands changed during focused_sweep."))
    state = _with_focused_sweep_record(state, record)
    write_workflow_state(cfg.tasks_dir, state)
    print(f"  focused_sweep evidence: {record.status} (exit {record.exit_code})")
    return state, record, False


def _focused_sweep_prompt(state: WorkflowState) -> str:
    record = _focused_sweep_record(state)
    if record is None:
        return ""
    return (
        "\nFOCUSED SWEEP EVIDENCE\n"
        f"Status: {record.status}\n"
        f"Source/command identity: {record.identity}\n"
        f"Command: {record.command}\n"
        f"Exit code: {record.exit_code}\n"
        f"Summary:\n{record.summary}\n"
        "This is plan-focused evidence only. It creates no verification "
        "receipt and cannot authorize accept.\n")


def _focused_sweep_completion_refusal(
        cfg: Config, plan: Plan, state: WorkflowState) -> str | None:
    record = _focused_sweep_record(state)
    if record is None:
        return None
    refreshed = _refresh_focused_sweep_evidence(cfg, plan, state)
    current = _focused_sweep_record(refreshed)
    assert current is not None
    if current.status == "PASSED":
        return None
    return (f"focused_sweep action evidence is {current.status} "
            f"(exit {current.exit_code}); a later focused_sweep must pass")


def _focused_test_identity(cfg: Config, task: Task) -> str:
    """Bind focused evidence to the checked-out source and exact task command."""
    if not gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        raise AssentError("source worktree is dirty at the focused_test boundary")
    command_sha256 = hashlib.sha256(task.verify.encode("utf-8")).hexdigest()
    return f"{gitops.tree_of(cfg.root, 'HEAD')}:{command_sha256}"


def _focused_test_record(state: WorkflowState) -> _TestActionEvidence | None:
    encoded = next((item[len(_TASK_FOCUSED_TEST_PREFIX):]
                    for item in reversed(state.focused_evidence)
                    if item.startswith(_TASK_FOCUSED_TEST_PREFIX)), None)
    if encoded is None:
        return None
    try:
        data = json.loads(encoded)
        record = _TestActionEvidence(
            data["status"], data["identity"], data["exit_code"],
            data["command"], data["summary"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise AssentError("Task focused_test action evidence is unreadable") from error
    if (record.status not in {"PASSED", "FAILED", "STALE"}
            or not record.identity or not isinstance(record.exit_code, int)
            or not isinstance(record.command, str)
            or not isinstance(record.summary, str)):
        raise AssentError("Task focused_test action evidence has invalid values")
    return record


def _with_focused_test_record(
        state: WorkflowState, record: _TestActionEvidence) -> WorkflowState:
    data = json.dumps({
        "status": record.status,
        "identity": record.identity,
        "exit_code": record.exit_code,
        "command": record.command,
        "summary": record.summary,
    }, ensure_ascii=False, separators=(",", ":"))
    ordinary = tuple(
        item for item in state.focused_evidence
        if not item.startswith(_TASK_FOCUSED_TEST_PREFIX))
    return replace(
        state, focused_evidence=ordinary + (_TASK_FOCUSED_TEST_PREFIX + data,))


def _refresh_focused_test_evidence(
        cfg: Config, task: Task, state: WorkflowState) -> WorkflowState:
    record = _focused_test_record(state)
    if record is None or record.status == "STALE":
        return state
    try:
        current = _focused_test_identity(cfg, task)
    except AssentError:
        current = ""
    if current == record.identity:
        return state
    return _with_focused_test_record(
        state, replace(
            record, status="STALE",
            summary="Source or focused command changed after focused_test."))


def _refresh_task_action_evidence(
        cfg: Config, task: Task, state: WorkflowState) -> WorkflowState:
    """Refresh every source-bound action result retained by a task workflow."""
    return _refresh_focused_test_evidence(cfg, task, state)


def _run_focused_test_action(
        cfg: Config, task: Task,
        state: WorkflowState) -> tuple[WorkflowState, _TestActionEvidence, bool]:
    """Run or reuse the task's source-bound focused command."""
    identity = _focused_test_identity(cfg, task)
    existing = _focused_test_record(state)
    if (existing is not None and existing.identity == identity
            and existing.status in {"PASSED", "FAILED"}):
        print(f"  focused_test {existing.status.lower()} evidence reused "
              f"(exit {existing.exit_code})")
        return state, existing, True

    armed = _TestActionEvidence(
        "STALE", identity, 0, task.verify,
        "Focused command has not completed for this source identity.")
    state = _with_focused_test_record(state, armed)
    write_workflow_state(cfg.tasks_dir, state)
    try:
        result = _verify_subprocess(cfg, task.verify)
    except OSError as error:
        record = replace(
            armed, status="FAILED", exit_code=1,
            summary=verification_summary(
                f"Unable to start focused command: {error}"))
    else:
        _show_verify_result(task.verify, result)
        fallback = ("Focused command passed." if result.returncode == 0 else
                    f"Focused command failed (exit code {result.returncode}).")
        record = replace(
            armed, status="PASSED" if result.returncode == 0 else "FAILED",
            exit_code=result.returncode,
            summary=verification_summary(result.stdout, result.stderr, fallback))
        try:
            unchanged = _focused_test_identity(cfg, task) == identity
        except AssentError:
            unchanged = False
        if not unchanged:
            record = replace(
                record, status="STALE",
                summary=verification_summary(
                    record.summary,
                    "Source or focused command changed during focused_test."))
    state = _with_focused_test_record(state, record)
    write_workflow_state(cfg.tasks_dir, state)
    print(f"  focused_test evidence: {record.status} (exit {record.exit_code})")
    return state, record, False


def _focused_test_prompt(state: WorkflowState) -> str:
    record = _focused_test_record(state)
    if record is None:
        return ""
    return (
        "\nFOCUSED TEST EVIDENCE\n"
        f"Status: {record.status}\n"
        f"Source/command identity: {record.identity}\n"
        f"Command: {record.command}\n"
        f"Exit code: {record.exit_code}\n"
        f"Summary:\n{record.summary}\n")


def _task_review_record(state: WorkflowState) -> auto_fix.ReviewRecord | None:
    encoded = next((item[len(_TASK_REVIEW_PREFIX):]
                    for item in reversed(state.focused_evidence)
                    if item.startswith(_TASK_REVIEW_PREFIX)), None)
    return auto_fix.parse_review_output(encoded) if encoded is not None else None


def _with_task_review_record(
        state: WorkflowState,
        record: auto_fix.ReviewRecord) -> WorkflowState:
    ordinary = tuple(
        item for item in state.focused_evidence
        if not item.startswith(_TASK_REVIEW_PREFIX))
    return replace(
        state, focused_evidence=(ordinary + (
            _TASK_REVIEW_PREFIX + auto_fix.review_record_json(record),)))


def _task_review_prompt(state: WorkflowState) -> str:
    record = _task_review_record(state)
    if record is None:
        return ""
    return (
        "\nPRIOR TASK VERDICT EVIDENCE\n"
        + auto_fix.review_record_json(record) + "\n")


def _task_blocker_evidence(state: WorkflowState) -> dict[str, str] | None:
    encoded = next((item[len(_TASK_BLOCKER_PREFIX):]
                    for item in reversed(state.focused_evidence)
                    if item.startswith(_TASK_BLOCKER_PREFIX)), None)
    if encoded is None:
        return None
    try:
        data = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise AssentError("Task BLOCKED evidence is unreadable") from error
    if (not isinstance(data, dict)
            or set(data) != {"role", "summary", "detail"}
            or not all(isinstance(data.get(key), str)
                       for key in ("role", "summary", "detail"))):
        raise AssentError("Task BLOCKED evidence has invalid values")
    return data


def _with_task_blocker_evidence(
        state: WorkflowState, role: str,
        summary: str, detail: str) -> WorkflowState:
    ordinary = tuple(
        item for item in state.focused_evidence
        if not item.startswith(_TASK_BLOCKER_PREFIX))
    data = json.dumps(
        {"role": role, "summary": summary, "detail": detail},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return replace(
        state, focused_evidence=ordinary + (_TASK_BLOCKER_PREFIX + data,))


def _task_blocker_prompt(state: WorkflowState) -> str:
    evidence = _task_blocker_evidence(state)
    if evidence is None:
        return ""
    return (
        "\nPRIOR TASK ROLE BLOCKED EVIDENCE\n"
        f"Role: {evidence['role']}\n"
        f"Summary: {evidence['summary']}\n"
        f"Detail:\n{evidence['detail']}\n")


def _task_blocked_continuation(
        workflow: tuple[WorkflowTaskStep | WorkflowActionStep, ...],
        current: int) -> int | None:
    """Return the next task-local role authorized to handle BLOCKED."""
    step = workflow[current]
    if isinstance(step, WorkflowTaskStep) and step.produces_verdict:
        following = current + 1
        if (following < len(workflow)
                and isinstance(workflow[following], WorkflowTaskStep)
                and workflow[following].writes
                and not workflow[following].produces_verdict):
            return following
        return None
    return next((
        index for index in range(current + 1, len(workflow))
        if isinstance(workflow[index], WorkflowTaskStep)
        and workflow[index].produces_verdict), None)


def _task_scope_transactions(state: WorkflowState) -> tuple[dict, ...]:
    transactions: list[dict] = []
    for item in state.focused_evidence:
        if not item.startswith(_TASK_SCOPE_AMENDMENT_PREFIX):
            continue
        try:
            data = json.loads(item[len(_TASK_SCOPE_AMENDMENT_PREFIX):])
        except json.JSONDecodeError as error:
            raise AssentError("Task scope-amendment state is unreadable") from error
        expected = {"task_id", "paths", "path_states", "fingerprints",
                    "before_sha256", "after_sha256"}
        if (not isinstance(data, dict) or set(data) != expected
                or data.get("task_id") != state.task_id
                or not isinstance(data.get("paths"), list)
                or not data["paths"]
                or not all(isinstance(path, str) and path for path in data["paths"])
                or len(data["paths"]) != len(set(data["paths"]))
                or not isinstance(data.get("path_states"), list)
                or len(data["path_states"]) != len(data["paths"])
                or not all(value in auto_fix.SCOPE_PATH_STATES
                           for value in data["path_states"])
                or not isinstance(data.get("fingerprints"), list)
                or len(data["fingerprints"]) != len(data["paths"])
                or not all(re.fullmatch(r"[0-9a-f]{64}", value or "")
                           for value in data["fingerprints"])
                or not re.fullmatch(r"[0-9a-f]{64}",
                                    data.get("before_sha256", ""))
                or not re.fullmatch(r"[0-9a-f]{64}",
                                    data.get("after_sha256", ""))):
            raise AssentError("Task scope-amendment state has invalid values")
        transactions.append(data)
    return tuple(transactions)


def _with_task_scope_transaction(
        state: WorkflowState, task: Task,
        additions: tuple[auto_fix.ApprovedScopeAddition, ...]) -> WorkflowState:
    if not additions:
        return state
    text = task.path.read_text(encoding="utf-8")
    paths = [item.path for item in additions]
    after = scope_text_with_entries(text, paths)
    data = {
        "task_id": task.id,
        "paths": paths,
        "path_states": [item.path_state for item in additions],
        "fingerprints": [item.fingerprint for item in additions],
        "before_sha256": task_text_sha256(text),
        "after_sha256": task_text_sha256(after),
    }
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"),
                         sort_keys=True)
    return replace(
        state, focused_evidence=(state.focused_evidence
                                 + (_TASK_SCOPE_AMENDMENT_PREFIX + encoded,)))


def _recover_task_scope_amendments(
        cfg: Config, state: WorkflowState,
        now: Callable[[], datetime]) -> Task:
    """Complete scheduler-owned task scope transactions recorded at role closeout."""
    task = Plan.parse(cfg.tasks_dir).get(state.task_id)
    if task is None:
        raise AssentError(
            f"Task scope-amendment owner disappeared: {state.task_id}")
    for transaction in _task_scope_transactions(state):
        text = task.path.read_text(encoding="utf-8")
        digest = task_text_sha256(text)
        paths = transaction["paths"]
        if digest == transaction["before_sha256"]:
            add_scope_entries(
                task.path, paths,
                expected_sha256=transaction["before_sha256"])
            task = parse_task_file(task.path)
        elif digest == transaction["after_sha256"]:
            if task.scope[-len(paths):] != paths:
                raise AssentError(
                    "Applied task scope amendment is not the exact suffix")
        else:
            raise AssentError(
                "Task contract drifted across its workflow scope amendment")

        encoded = json.dumps(transaction, ensure_ascii=False,
                             separators=(",", ":"), sort_keys=True)
        transaction_id = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        marker = f"transaction_sha256: {transaction_id}"
        if not any(entry.get("event") == _SCOPE_AMENDMENT_EVENT
                   and marker in str(entry.get("detail", ""))
                   for entry in read_entries(task.journal_path)):
            append_entry(
                task.journal_path, by="scheduler",
                event=_SCOPE_AMENDMENT_EVENT,
                summary=_SCOPE_AMENDMENT_SUMMARY,
                detail=(marker + "\npaths: "
                        + json.dumps(paths, ensure_ascii=False,
                                     separators=(",", ":"))),
                time_str=now().isoformat(timespec="seconds"))
    return parse_task_file(task.path)


def _focused_test_completion_refusal_for_record(
        record: _TestActionEvidence | None) -> str | None:
    if record is not None and record.status == "PASSED":
        return None
    if record is None:
        return ("focused_test action has no durable evidence; a later explicit "
                "focused_test must pass")
    return (f"focused_test action evidence is {record.status} "
            f"(exit {record.exit_code}); a later explicit focused_test must pass")


def _focused_test_completion_refusal(state: WorkflowState) -> str | None:
    return _focused_test_completion_refusal_for_record(
        _focused_test_record(state))


def _block_task_action(
        cfg: Config, task: Task, record: _TestActionEvidence, reason: str,
        now: Callable[[], datetime]) -> None:
    set_status(task.path, "BLOCKED")
    append_entry(
        task.journal_path, by="scheduler", event="blocked",
        summary=f"Scheduler marked BLOCKED: {reason}",
        detail=(f"Command: {record.command}\nExit code: {record.exit_code}\n"
                f"Summary:\n{record.summary}"),
        time_str=now().isoformat(timespec="seconds"))
    workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
    gitops.commit_if_dirty(
        cfg.root, _checkpoint_subject(
            cfg, "auto", task, f"BLOCKED - {_short(reason, 50)}"),
        cfg.git_excludes)


def _role_policy(step: WorkflowTaskStep | object) -> str:
    resolved = step.resolved_role
    return "\n\n".join(ability.prompt for ability in resolved.abilities)


def _workflow_task_session(
        cfg: Config, adapter: Adapter, task: Task, step: WorkflowTaskStep,
        adapter_name: str) -> SessionIdentity:
    """Resolve one configured task role, falling back to the task's profile."""
    model = step.resolved_role.model or task.model
    stated_effort = inherited_effort(
        step.resolved_role.model, step.resolved_role.effort, task.effort)
    settings = cfg.adapter_settings(adapter_name)
    effort = settings.resolve_effort(stated_effort, model)
    return SessionIdentity(
        agent=adapter_name,
        requested_model=adapter.resolve_model(model),
        effort=effort,
        requested_effort=settings.resolve_requested_effort(model, effort),
    )


def _effective_task_workflow(
        cfg: Config, task: Task
        ) -> tuple[WorkflowTaskStep | WorkflowActionStep, ...] | None:
    """Resolve a task override, or inherit the project task workflow when omitted."""
    if task.workflow is None:
        return cfg.workflow_task
    steps: list[WorkflowTaskStep | WorkflowActionStep] = []
    for index, entry in enumerate(task.workflow):
        if isinstance(entry, TaskWorkflowAction):
            steps.append(WorkflowActionStep(entry.action))
            continue
        try:
            resolved = cfg.resolve_role(entry)
        except AssentError as error:
            raise AssentError(
                f"Task {task.id} workflow[{index}] names missing agent role {entry!r}") from error
        steps.append(WorkflowTaskStep(entry, resolved))
    resolved_steps = tuple(steps)
    validate_task_workflow_steps(resolved_steps)
    return resolved_steps


def _workflow_task_capability_errors(
        cfg: Config, adapter: Adapter, plan: Plan, adapter_name: str,
        task_id: str | None) -> list[str]:
    requests: list[InvocationRequest] = []
    try:
        for task in plan.tasks:
            if (task.status not in ("TODO", "WIP")
                    or (task_id is not None and task.id != task_id)):
                continue
            errors = literal_adapter_errors(cfg, task)
            if errors:
                return errors
            workflow = _effective_task_workflow(cfg, task)
            if workflow is None:
                session = resolve_session(cfg, adapter, task, adapter_name)
                requests.append(InvocationRequest(
                    task_id=task.id, model=task.model, effort=session.effort,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort))
                continue
            if not workflow:
                task_plan = Plan([task], plan.dir)
                for index, step in enumerate(cfg.workflow_plan):
                    if isinstance(step, WorkflowActionStep):
                        continue
                    if adapter_name not in step.adapters:
                        continue
                    session = _plan_step_session(
                        cfg, adapter, task_plan, step, adapter_name)
                    requests.append(InvocationRequest(
                        task_id=f"{task.id} workflow.plan[{index}]",
                        model=step.model or task.model,
                        effort=session.effort,
                        requested_model=session.requested_model,
                        requested_effort=session.requested_effort))
                continue
            for index, step in enumerate(workflow):
                if isinstance(step, WorkflowActionStep):
                    continue
                if (step.adapters is not None
                        and adapter_name not in step.adapters):
                    continue
                session = _workflow_task_session(
                    cfg, adapter, task, step, adapter_name)
                requests.append(InvocationRequest(
                    task_id=f"{task.id} workflow[{index}]",
                    model=step.resolved_role.model or task.model,
                    effort=session.effort,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort))
    except AssentError as error:
        return [str(error)]
    return adapter.preflight(requests)


def _plan_step_session(
        cfg: Config, adapter: Adapter, plan: Plan, step: WorkflowPlanStep,
        adapter_name: str) -> SessionIdentity:
    """Resolve a whole-plan worker step through its role or the first task profile."""
    first = plan.tasks[0]
    model = step.model or first.model
    stated_effort = (
        step.effort
        if step.model is not None and step.produces_verdict
        else inherited_effort(step.model, step.effort, first.effort))
    settings = cfg.adapter_settings(adapter_name)
    effort = settings.resolve_effort(stated_effort, model)
    if (step.requested_model is not None
            and adapter_name == step.adapter):
        return SessionIdentity(
            agent=step.adapter or adapter_name,
            requested_model=step.requested_model,
            effort=effort,
            requested_effort=step.requested_effort)
    return SessionIdentity(
        agent=adapter_name,
        requested_model=adapter.resolve_model(model),
        effort=effort,
        requested_effort=settings.resolve_requested_effort(model, effort))


def _plan_workflow_capability_errors(
        cfg: Config, adapter: Adapter, plan: Plan,
        adapter_name: str) -> list[str]:
    requests: list[InvocationRequest] = []
    try:
        for task in plan.tasks:
            errors = literal_adapter_errors(cfg, task)
            if errors:
                return errors
        for index, step in enumerate(cfg.workflow_plan):
            if isinstance(step, WorkflowActionStep):
                continue
            if adapter_name not in step.adapters:
                continue
            session = _plan_step_session(
                cfg, adapter, plan, step, adapter_name)
            requests.append(InvocationRequest(
                task_id=f"{cfg.tasks_name} workflow.plan[{index}]",
                model=step.model or plan.tasks[0].model,
                effort=session.effort,
                requested_model=session.requested_model,
                requested_effort=session.requested_effort))
    except AssentError as error:
        return [str(error)]
    return adapter.preflight(requests)


def _task_workflow_suffix(
        step: WorkflowTaskStep, index: int, total: int) -> str:
    final = index == total - 1
    closeout = (
        "This is the final task workflow step; follow the ordinary task closeout "
        "instructions above."
        if final else
        "More task workflow steps remain. Do not mark the task DONE. Leave its "
        "status WIP (or mark BLOCKED only for a genuine blocker), and append this "
        "session's one journal entry before returning. The scheduler advances the "
        "derived workflow cursor."
    )
    suffix = _TASK_WORKFLOW_SUFFIX.format(
        position=index + 1, total=total, role=step.role,
        role_policy=_role_policy(step), closeout_policy=closeout)
    if step.produces_verdict:
        write_policy = (
            "This is a write-capable review-and-repair session. Diagnose and "
            "repair every reported blocker now, then return FIXED. If the exact "
            "repair cannot be completed, make no source write and return FAIL."
            if step.writes else
            "This is a read-only decision session. Make no source write, return "
            "FAIL with the blocking findings, and leave repair to the separately "
            "configured writable role.")
        suffix += _TASK_VERDICT_POLICY.format(write_policy=write_policy)
    return suffix


def _build_prompt(cfg: Config, task: Task, failure_reason: str | None,
                  session: SessionIdentity, resumed: bool = False, *,
                  workflow_step: WorkflowTaskStep | None = None,
                  workflow_index: int = 0,
                  workflow_total: int = 0,
                  action_evidence: str = "",
                  focused_test_action_present: bool = False) -> str:
    focused_test_policy = (
        "Do not run the task's focused command in this AI session. The "
        "scheduler-owned focused_test action runs it at its explicit workflow "
        "position and owns its evidence, retry, and checkpoint decision."
        if focused_test_action_present else
        f"To verify yourself, run this in the current working tree: {task.verify}\n"
        "This is the focused task gate. If an outer tool times out while the child result is\n"
        "unknown, do not start a concurrent duplicate and do not mark the task BLOCKED solely\n"
        "because of that timeout; determine the child result serially. The scheduler runs the\n"
        "same command after the AI session and owns the checkpoint/retry decision."
    )
    text = (_PROMPT_TEMPLATE
            .replace("{agents_md_path}", _agents_md_path_for_prompt(cfg))
            .replace("{instructions_path}", str(contracts.instructions_path()))
            .replace("{task_path}", cfg.rel(task.path))
            .replace("{journal_path}", cfg.rel(task.journal_path))
            .replace("{verify_command}", task.verify)
            .replace("{task_id}", task.id)
            .replace("{task_title}", task.title)
            .replace("{agent}", session.agent)
             .replace("{requested_model}", session.requested_model)
             .replace("{effort}", effort_identity(session.effort))
             .replace("{requested_effort}",
                      effort_identity(session.requested_effort))
             .replace("{focused_test_policy}", focused_test_policy))
    # Startup provisioning already reports a classification failure before an
    # adapter is normally reached.  Prompt construction remains best-effort so
    # that a pre-session failure does not acquire a duplicated clause; the
    # closeout gate below handles the same error fail-closed.
    try:
        contract = _shared_paths_contract(cfg)
    except AssentError:
        contract = None
    if contract is not None:
        text += shared_paths.review_clause(contract)
    if resumed:
        text += _RESUME_SUFFIX
    else:
        text += _rework_prompt_suffix(task)
    if failure_reason and failure_reason.startswith(_CLOSEOUT_ONLY_MARKER):
        text += _CLOSEOUT_RETRY_SUFFIX.replace("{verify_command}", task.verify)
    elif failure_reason:
        text += _RETRY_SUFFIX.replace("{failure_reason}", failure_reason)
    if workflow_step is not None:
        text += _task_workflow_suffix(
            workflow_step, workflow_index, workflow_total)
    text += action_evidence
    return text


def _rework_prompt_suffix(task: Task) -> str:
    """Carry a rejection reason into the next TODO run of a task the human just reworked.

    Triggers only when the task's journal's last entry is a pending ``rework_requested``
    record (written by ``rework.py``, never modified here). Any read/parse problem, or a
    reason that cannot be located in that entry's detail text, degrades to a warning-only
    suffix (or none at all, if the trigger condition itself cannot be established) rather than
    failing prompt assembly.
    """
    try:
        entries = read_entries(task.journal_path)
    except AssentError:
        return ""
    if not entries or entries[-1].get("event") != "rework_requested":
        return ""
    detail = entries[-1].get("detail")
    reason = None
    if isinstance(detail, str):
        m = _REWORK_REASON_RE.search(detail)
        if m:
            reason = m.group(1).strip()
    clause = f" Rejection reason: {reason}" if reason else ""
    return _REWORK_SUFFIX.replace("{reject_reason_clause}", clause)


def _session_line(adapter_name: str, task: Task,
                  session: SessionIdentity,
                  model: str | None = None) -> str:
    """The one opening line that states the whole resolved session identity.

    Four facts, in the order they are decided: which adapter runs, and each selected choice
    beside the concrete value actually sent to that adapter's CLI, e.g.
    ``Session: codex | core->gpt-5.6-luna | heavy->max``.
    """
    return (f"  Session: {adapter_name} | {model or task.model}->{session.requested_model}"
            f" | {effort_identity(session.effort)}->"
            f"{effort_identity(session.requested_effort)}")


def _short(text: str, limit: int = 60) -> str:
    """Squash to a single line and truncate, for use in a commit message."""
    return " ".join(text.split())[:limit]


def _bounded_adapter_diagnostic(output: str,
                                limit: int = _ADAPTER_DIAGNOSTIC_LIMIT) -> str:
    """Return a one-line, bounded diagnostic suitable for prompts and journals.

    Adapter output remains available live to the operator, but persisted scheduler evidence
    must not contain an arbitrary-length transcript, prompt, or common credential forms.
    Redaction happens before truncation so a token is not exposed merely because its label
    fell outside the retained tail.
    """
    redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1[REDACTED]", output)
    redacted = _BEARER_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _KNOWN_TOKEN_RE.sub("[REDACTED]", redacted)
    compact = " ".join(redacted.split())
    if not compact:
        return "no diagnostic output"
    if len(compact) <= limit:
        return compact
    return "..." + compact[-(limit - 3):]


def _adapter_failure_reason(exit_code: int, stalled: bool, output: str,
                            failure_kind: str | None = None) -> str:
    kind = "watchdog stalled" if stalled else "exited nonzero"
    diagnostic = _bounded_adapter_diagnostic(output)
    # An adapter may classify its own failure (quota, permission, unsupported model, ...).
    # It is recorded as the adapter's reading of the evidence; the verdict stays the
    # scheduler's, so the exit code and the diagnostic are still reported in full.
    label = f" classified by the adapter as {failure_kind}" if failure_kind else ""
    return (f"Adapter process {kind} (exit code {exit_code}){label}; "
            f"bounded diagnostic: {diagnostic}")


def _append_adapter_failure_entry(task: Task, session: SessionIdentity,
                                  exit_code: int, stalled: bool, summary: str,
                                  now: Callable[[], datetime],
                                  failure_kind: str | None = None) -> None:
    """Append the machine-readable scheduler evidence for one non-quota failure.

    This event needs a numeric ``exit_code`` in addition to the shared journal fields.  It is
    written here because the general journal helper deliberately exposes only the stable
    common schema.  Re-reading validates the complete TOML document after the append.
    """
    fields = [
        "[[entry]]",
        f"time = {json.dumps(now().isoformat(timespec='seconds'))}",
        'by = "scheduler"',
        f"agent = {json.dumps(session.agent)}",
        f"requested_model = {json.dumps(session.requested_model)}",
    ]
    if session.requested_effort is not None:
        fields.append(
            f"requested_effort = {json.dumps(session.requested_effort)}")
    fields += [
        f"event = {json.dumps('adapter_stall' if stalled else 'adapter_exit')}",
        f"exit_code = {int(exit_code)}",
        f"stalled = {'true' if stalled else 'false'}",
    ]
    if failure_kind:
        fields.append(f"failure_kind = {json.dumps(failure_kind)}")
    fields.append(f"summary = {json.dumps(summary, ensure_ascii=False)}")
    block = "\n".join(fields) + "\n"
    existing = (task.journal_path.read_text(encoding="utf-8")
                if task.journal_path.is_file() else "")
    with open(task.journal_path, "a", encoding="utf-8", newline="") as journal:
        if existing and not existing.endswith("\n"):
            journal.write("\n")
        if existing:
            journal.write("\n")
        journal.write(block)
    read_entries(task.journal_path)


def _checkpoint_subject(cfg: Config, kind: str, task: Task, detail: str) -> str:
    """Build a task checkpoint subject namespaced by the task folder."""
    return f"{kind}({cfg.tasks_name}/{task.id}): {detail}"


def _task_excludes(cfg: Config, task: Task) -> list[str]:
    """The scope-check exemption list for this task: its own t file and r file (the status
    update and journal are part of the job) plus the global runtime artifacts."""
    return [cfg.git_rel(task.path), cfg.git_rel(task.journal_path),
            *cfg.git_excludes]


def _agents_md_path_for_prompt(cfg: Config) -> str:
    """Choose the project rules: prefer the branch version, else fall back to the main
    working tree's absolute path."""
    candidate = cfg.root / "AGENTS.md"
    if candidate.is_file():
        return cfg.rel(candidate)
    if cfg.source_root is not None:
        source = cfg.source_root / "AGENTS.md"
        if source.is_file():
            return str(source.resolve())
    return "AGENTS.md (if present; skip if absent)"


def _agents_md_absolute_path_for_prompt(cfg: Config) -> str:
    """Return an unambiguous rules path for a session spanning worktrees."""
    candidate = cfg.root / "AGENTS.md"
    if candidate.is_file():
        return str(candidate.resolve())
    if cfg.source_root is not None:
        source = cfg.source_root / "AGENTS.md"
        if source.is_file():
            return str(source.resolve())
    return "AGENTS.md (if present; skip if absent)"


def _require_stack_ancestry(cfg: Config, state: StackState,
                            downstream_tip: str) -> None:
    """Require the downstream tip to contain the current declared base, if any."""
    source = state.base.speculative_upstream
    if source is None or gitops.is_ancestor(cfg.root, source.tip, downstream_tip):
        return
    raise AssentError(
        f"stale stack for {cfg.tasks_name}: current upstream "
        f"{source.folder} tip {source.tip} is not an ancestor of downstream "
        f"tip {downstream_tip}; all existing work is preserved. Run `assent "
        f"rework {cfg.tasks_name}` after deciding how to handle the upstream "
        "change, or replan the dependency")


def _prepare_worktree(cfg: Config) -> Config:
    """Create or validate the folder worktree before any adapter is started."""
    errors = worktree_configuration_errors(cfg)
    if errors:
        detail = "\n".join(f"  - {error}" for error in errors)
        raise AssentError(f"git worktree version-control layering error:\n{detail}")

    state_before = resolve_stack_state(cfg)
    candidate = gitops.worktree_path(cfg.root, cfg.tasks_name)
    existed_before = candidate.exists()
    try:
        root = gitops.ensure_worktree(
            cfg.root, cfg.tasks_name, state_before.base.resolved_base)
        worktree_cfg = cfg.for_worktree(root)

        if existed_before:
            # A reused worktree may still hold uncommitted work from a prior run
            # that exited uncleanly (hard power loss / kill) without ever reaching
            # an interrupt handler.  Whether that dirty state is clean, provably
            # attributable and recoverable, or a fail-closed refusal is decided by
            # the single run-startup gate in _run_locked below -- never discarded
            # here.  The branch/ancestry checks that follow read only committed
            # HEAD state, so they are unaffected by an uncommitted working tree.
            branch = gitops.current_branch(worktree_cfg.root)
            if not branch:
                raise AssentError(
                    f"existing worktree {root} is detached; refusing to switch or "
                    "rewrite it automatically")
            if not branch.startswith(worktree_cfg.branch_prefix):
                raise AssentError(
                    f"existing worktree {root} is on foreign branch {branch}; "
                    f"expected prefix {worktree_cfg.branch_prefix}")
        else:
            # A freshly created worktree is a clean checkout of the base commit; any
            # dirt here is a genuine setup fault, so keep failing closed immediately.
            gitops.ensure_clean(worktree_cfg.root, worktree_cfg.git_excludes)
            branch = gitops.ensure_branch(
                worktree_cfg.root, worktree_cfg.branch_prefix)

        downstream_tip = gitops.commit_of(worktree_cfg.root, "HEAD")
        if not branch.startswith(worktree_cfg.branch_prefix):
            raise AssentError(
                f"worktree branch {branch or '(detached)'} does not use required "
                f"prefix {worktree_cfg.branch_prefix}")
        if not existed_before and downstream_tip != state_before.base.resolved_base:
            raise AssentError(
                f"new worktree HEAD {downstream_tip} does not match resolved base "
                f"{state_before.base.resolved_base}")

        state_after = resolve_stack_state(cfg)
        tips_before = {source.folder: source.tip for source in state_before.sources}
        tips_after = {source.folder: source.tip for source in state_after.sources}
        if tips_after != tips_before:
            changes = sorted(set(tips_before) | set(tips_after))
            detail = ", ".join(
                f"{folder}: {tips_before.get(folder, '(missing)')} -> "
                f"{tips_after.get(folder, '(missing)')}"
                for folder in changes
                if tips_before.get(folder) != tips_after.get(folder))
            raise AssentError(
                "upstream source changed between stack resolution and worktree "
                f"validation ({detail})")
        _require_stack_ancestry(cfg, state_after, downstream_tip)

        # REVIEWED-PATHS provisions every declared missing link before any
        # adapter starts; REVIEWED-NONE starts with no links and no extra AI
        # instructions, and UNKNOWN or STALE touches the filesystem not at all.
        contract = shared_paths.prepare_worktree(
            gitops.main_worktree(cfg.root), worktree_cfg.root,
            required_evidence=_diagnosed_shared_inputs(cfg))
        print(shared_paths.describe(contract))
        print(f"Isolated worktree: {root}")
        print(f"Target snapshot: {state_after.base.target_snapshot}")
        stacked = state_before.base.speculative_upstream
        if stacked is None:
            print("Stacked upstream: none")
        else:
            print(f"Stacked upstream: {stacked.folder} @ {stacked.tip}")
        print(f"Work branch: {branch}")
        return worktree_cfg
    except BaseException as primary_error:
        # Only a path absent before this call can belong to this setup attempt.
        # Existing worktrees are never cleanup candidates, even when invalid.
        if not existed_before and candidate.exists():
            try:
                gitops.cleanup_unstarted_worktree(
                    cfg.root, cfg.tasks_name,
                    state_before.base.resolved_base, cfg.branch_prefix)
            except AssentError as cleanup_error:
                raise AssentError(
                    f"worktree setup failed ({primary_error}); cleanup was "
                    "incomplete and resources were retained for recovery: "
                    f"{cleanup_error}") from primary_error
        raise


# --------------------------------------------------------------------------- #
# run: main loop
# --------------------------------------------------------------------------- #
def _selection_snapshot(configs: tuple[Config, ...]) -> tuple[
        str, str, tuple[str, ...]]:
    """Read the exact target and source identity for a completed selection."""
    main = gitops.main_worktree(configs[0].root)
    target_ref = gitops.require_current_branch(main)
    target_commit = gitops.commit_of(main, target_ref)
    sources: list[str] = []
    for cfg in configs:
        plan = Plan.parse(cfg.tasks_dir)
        unfinished = [f"{task.id}={task.status}" for task in plan.tasks
                      if task.status not in ("DONE", "SKIP")]
        if unfinished:
            raise AssentError(
                f"selection folder {cfg.tasks_name} is incomplete: "
                + ", ".join(unfinished))
        _branch, tip, _worktree = source_snapshot(cfg, main)
        sources.append(tip)
    return target_ref, target_commit, tuple(sources)


@contextlib.contextmanager
def _selection_locks(configs: tuple[Config, ...]):
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            lockfile.hold_integration_lock(configs[0].assent_dir))
        for cfg in configs:
            stack.enter_context(
                lockfile.hold_lock(cfg.tasks_dir, cfg.tasks_name))
        yield


def _selection_worktree_configs(
        configs: tuple[Config, ...]) -> tuple[Config, ...]:
    """Resolve the persistent source worktree for every selected folder."""
    main = gitops.main_worktree(configs[0].root)
    resolved: list[Config] = []
    for cfg in configs:
        _branch, _tip, worktree = source_snapshot(cfg, main)
        if worktree is None:
            raise AssentError(
                f"selection repair requires the source worktree for "
                f"{cfg.tasks_name}")
        resolved.append(cfg.for_worktree(worktree))
    return tuple(resolved)


def _selection_bounded(text: str) -> str:
    if len(text) <= _AUTO_FIX_EVIDENCE_LIMIT:
        return text
    return text[:_AUTO_FIX_EVIDENCE_LIMIT] + "\n... [evidence truncated]"


def _selection_review_material(
        work_configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: WorkflowPlanStep, *, repair_workspaces: str = "") -> tuple[str, str]:
    """Build the source-bound verifier review prompt and its digest."""
    focused: list[str] = []
    diffs: list[str] = []
    management: list[str] = []
    prior: list[str] = []
    for cfg in work_configs:
        plan = Plan.parse(cfg.tasks_dir)
        focused.extend(
            f"- {cfg.tasks_name}/{task.id}: PASS before selection full_verify: "
            f"{task.verify}"
            for task in plan.tasks if task.status == "DONE")
        base = resolve_stack_state(cfg).base.resolved_base
        merge_base = gitops.merge_base(cfg.root, base, "HEAD")
        diffs.append(
            f"## {cfg.tasks_name}\n{_auto_fix_diff(cfg, merge_base)}")
        for task in plan.tasks:
            try:
                contract = task.path.read_text(encoding="utf-8")
                journal = (task.journal_path.read_text(encoding="utf-8")
                           if task.journal_path.exists() else "(no journal)")
            except OSError as error:
                raise AssentError(
                    f"selection review evidence is unreadable for "
                    f"{cfg.tasks_name}/{task.id}: {error}") from error
            management.append(
                f"## {cfg.tasks_name}/{task.id}\n"
                f"Authoritative contract:\n{contract}\nJournal:\n{journal}")
        path = auto_fix.auto_fix_state_path(cfg)
        if path.exists():
            previous = auto_fix.read_auto_fix_state(path)
            if previous.review_context == "selection_verification":
                current = set(previous.current_finding_fingerprints)
                lines = [
                    f"- {item.fingerprint} {cfg.tasks_name}/"
                    f"{item.task_id or 'unassigned'} {item.path}: {item.summary}"
                    for item in previous.findings
                    if item.fingerprint in current]
                prior.append(
                    f"## {cfg.tasks_name} ({previous.verdict}/"
                    f"{previous.phase})\n" + ("\n".join(lines) or "- none"))

    selection_evidence = "\n".join((
        f"Folders: {', '.join(state.folders)}",
        f"Target ref: {state.target_ref}",
        f"Target commit: {state.target_commit}",
        f"Source commits: {', '.join(state.source_commits)}",
        f"Candidate tree: {state.action_candidate_tree}",
        f"Verifier digest: {state.verification_script_sha256}",
        f"Shared-input digest: {state.shared_inputs_sha256}",
        f"Verifier exit code: {state.action_exit_code}",
        "Verifier outcome and bounded output:",
        *(f"- {item}" for item in state.action_evidence),
    ))
    prompt = _SELECTION_VERIFICATION_REVIEW_PROMPT.format(
        agents_md_paths="\n".join(
            f"- {_agents_md_absolute_path_for_prompt(cfg)}"
            for cfg in work_configs),
        instructions_path=contracts.instructions_path(),
        workflow_role=step.role,
        role_policy="\n".join(
            ability.prompt for ability in step.resolved_role.abilities),
        write_policy=(
            "This is a write-capable merged review-and-repair transaction. "
            "Inspect and edit only ordinary source files in the scheduler-owned "
            "workspaces listed below. Do not edit task, journal, Git, receipt, "
            "or management files and do not run tests or generators. For an "
            "exact omitted scope file, repair that path in this same session, "
            "return FIXED with scope_amendment, and let the scheduler validate "
            "and append the path at closeout. The scheduler owns every "
            "checkpoint and gate."
            if step.writes else
            "This is a read-only decision gate. Do not edit source, task, "
            "journal, Git, receipt, or management files and do not run tests "
            "or generators."),
        verdict_policy=(
            "Return FIXED with one or more schema-complete findings when this "
            "session repaired every reported blocker. Return FAIL only when "
            "you made no source edit and cannot complete the repair."
            if step.writes else
            "FIXED is not valid for this read-only role. Return FAIL with one "
            "or more schema-complete findings."),
        repair_workspaces=(repair_workspaces or
                           "- none (this role is read-only)"),
        selection_evidence=_selection_bounded(selection_evidence),
        focused_evidence=_selection_bounded(
            "\n".join(focused) or "- none"),
        cumulative_diffs=_selection_bounded("\n\n".join(diffs)),
        prior_evidence=_selection_bounded("\n\n".join(prior) or "- none"),
        management_evidence=_selection_bounded("\n\n".join(management)),
    )
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _selection_shared_path_review_material(
        cfg: Config, state: SelectionWorkflowState, step: WorkflowPlanStep,
        contract: "shared_paths.Contract", required: tuple[str, ...],
        failure_output: str, *, preparation: bool) -> tuple[str, str]:
    """Build one read-only recovery review for a contradicted shared profile."""
    trigger = (
        "Worktree preparation found that the current shared-path profile no "
        "longer agrees with the existing source-worktree link evidence."
        if preparation else
        "The focused closeout mechanically named a descendant of these "
        "ordinary ignored primary-worktree directories: "
        + ", ".join(required) + "."
        if required else
        "Focused closeout produced stale shared-path evidence.")
    required_policy = (
        "The paths must include every required directory named above."
        if required else
        "Return the complete path answer for this source snapshot.")
    prompt = f"""Before inspecting source, read the project rules
{_agents_md_absolute_path_for_prompt(cfg)} and the Assent session rules
{contracts.instructions_path()}.

This is a bounded, read-only shared-input recovery for exact selection
{', '.join(state.folders)} at workflow.integration[{state.step_index}].
{trigger}

The active shared-path answer is {contract.state}; its paths are
{', '.join(contract.paths) or '(none)'}. Decide the complete shared-directory
answer from the named failure, Git-ignore rules, and tracked dependency/build
declarations. Inspect only what this decision needs. Do not edit source, task,
journal, Git, receipt, manifest, or any shared-path link. Do not run tests,
generators, Git, or Assent commands. The scheduler alone applies and validates
the decision.

Configured role policy:
{chr(10).join(ability.prompt for ability in step.resolved_role.abilities)}

Focused failure evidence:
{_selection_bounded(failure_output)}

Finish with exactly one assent.auto_fix_review JSON object on the final
non-empty line. Return verdict PASS, no findings, and a non-null shared_paths
object containing exact paths and watch string lists. {required_policy} Watch
exactly the tracked dependency or build files whose change should invalidate
the decision.

{{"type":"assent.auto_fix_review","verdict":"PASS","shared_paths":{{"paths":[],"watch":[]}},"findings":[]}}
"""
    return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _selection_surface_baseline(cfg: Config):
    return (
        _auto_fix_surface_snapshot(cfg),
        gitops.commit_of(cfg.root, "HEAD"),
        gitops.working_tree_status(cfg.root, cfg.git_excludes),
        gitops.commit_of(cfg.source_root, "HEAD") if cfg.source_root else None,
        (gitops.working_tree_status(cfg.source_root, cfg.git_excludes)
         if cfg.source_root else None),
    )


def _selection_surface_changes(
        work_configs: tuple[Config, ...], baselines) -> tuple[str, ...]:
    changed: list[str] = []
    for cfg, baseline in zip(work_configs, baselines):
        paths = _auto_fix_surface_change(
            baseline[0], cfg, baseline[1], baseline[2],
            baseline[3], baseline[4])
        changed.extend(f"{cfg.tasks_name}:{path}" for path in paths)
    return tuple(dict.fromkeys(changed))


def _run_selection_reviewer(
        work_configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: WorkflowPlanStep, *, sleep: Callable[[float], None],
        now: Callable[[], datetime], repair_workspaces: str = "",
        shared_path_recovery: tuple[
            Config, "shared_paths.Contract", tuple[str, ...], str, bool
        ] | None = None,
        ) -> tuple[auto_fix.ReviewRecord, str, SessionIdentity]:
    """Run one selection verdict role against settled verifier evidence."""
    if (not state.action_evidence
            or state.action_evidence[0] not in {
                "VERIFIER_FAILED", "TARGET_CONFLICT", "PEER_CONFLICT"}):
        raise AssentError(
            "selection repair requires a durable verifier failure or candidate "
            "conflict action result")
    if not step.produces_verdict:
        raise AssentError(
            f"selection role {step.role!r} cannot produce the required verdict")
    rotation = _workflow_step_rotation(work_configs[0], step)
    sessions: dict[str, SessionIdentity] = {}
    for adapter_name, reviewer in zip(rotation.names, rotation.adapters):
        session = _plan_step_session(
            work_configs[0], reviewer,
            Plan.parse(work_configs[0].tasks_dir), step, adapter_name)
        errors = reviewer.preflight([InvocationRequest(
            task_id="selection-verification-review", model=step.model,
            effort=session.effort, requested_model=session.requested_model,
            requested_effort=session.requested_effort)])
        if errors:
            raise AssentError(
                f"selection reviewer capability unavailable for {adapter_name}: "
                + "; ".join(errors))
        sessions[adapter_name] = session
    if shared_path_recovery is None:
        prompt, prompt_digest = _selection_review_material(
            work_configs, state, step, repair_workspaces=repair_workspaces)
    else:
        recovery_cfg, contract, required, failure_output, preparation = (
            shared_path_recovery)
        prompt, prompt_digest = _selection_shared_path_review_material(
            recovery_cfg, state, step, contract, required, failure_output,
            preparation=preparation)
    baselines = tuple(
        _selection_surface_baseline(cfg) for cfg in work_configs)
    invalid_attempts = 0
    attempt_prompt = prompt
    failed_adapters: set[str] = set()
    while True:
        adapter_name = rotation.name
        reviewer = rotation.adapter
        session = sessions[adapter_name]
        label = ("Selection shared-path recovery session"
                 if shared_path_recovery is not None
                 else "Selection review session")
        print(f"{label}: {adapter_name} | "
              f"{step.model}->{session.requested_model} | "
              f"{effort_identity(session.effort)}->"
              f"{effort_identity(session.requested_effort)}")
        result = _invoke_adapter(
            work_configs[0], reviewer, adapter_name, attempt_prompt,
            session.requested_model, session.requested_effort,
            work_configs[0].root, context_kind="selection",
            context_id=(f"workflow.selection[{state.step_index}].shared_paths"
                        if shared_path_recovery is not None else
                        f"workflow.selection[{state.step_index}].review"),
            folders=state.folders, structured=True)
        interval_changes: list[str] = []
        forbidden: list[str] = []
        for cfg, baseline in zip(work_configs, baselines):
            changed = _auto_fix_surface_change(
                baseline[0], cfg, baseline[1], baseline[2],
                baseline[3], baseline[4])
            interval_changes.extend(
                f"{cfg.tasks_name}:{path}" for path in changed)
            forbidden.extend(
                f"{cfg.tasks_name}:{path}"
                for path in _auto_fix_forbidden_writes(changed))
        refused = (interval_changes
                   if shared_path_recovery is not None or not step.writes
                   else forbidden)
        if refused:
            raise AssentError(
                "selection reviewer wrote protected project paths; exact edits "
                "were preserved: " + ", ".join(refused[:8]))
        if (result.checkpoint_resume and not result.quota_exhausted
                and not result.stalled and result.exit_code != 0):
            continue
        if result.quota_exhausted:
            if len(rotation.names) == 1:
                _wait_for_quota(work_configs[0], result.reset_at, sleep, now)
            elif rotation.advance_after_quota(failed_adapters):
                _wait_for_rotation(work_configs[0], sleep)
            continue
        if result.failure_kind == "authentication":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            _checkpoint_selection_merged_writes(work_configs)
            action = _authentication_failover_action(
                rotation, failed_adapters, reason)
            if action == "switch":
                print("Selection reviewer authentication failure; switching "
                      f"{adapter_name} -> {rotation.name}.")
            else:
                print("Selection reviewer authentication failure; waiting only "
                      f"for recoverable adapter {rotation.name}.")
                _wait_for_rotation(work_configs[0], sleep)
            continue
        if result.exit_code != 0 or result.stalled:
            if _adapter_availability_failed(result):
                switched = rotation.advance_after_failure(failed_adapters)
                _checkpoint_selection_merged_writes(work_configs)
                if switched:
                    print(f"Selection reviewer adapter failure; switching "
                          f"{adapter_name} -> {rotation.name}.")
                else:
                    print("Selection reviewer adapters unavailable; waiting "
                          f"{work_configs[0].rotation_poll_minutes} minute(s) "
                          f"before restarting with {rotation.name}.")
                    _wait_for_rotation(work_configs[0], sleep)
                continue
            raise AssentError("selection reviewer adapter failure: "
                              + _adapter_failure_reason(
                                  result.exit_code, result.stalled,
                                  result.output, result.failure_kind))
        output = (result.structured_output
                  if result.structured_output is not None else result.output)
        try:
            if result.structured_output_error is not None:
                raise AssentError(result.structured_output_error)
            record = auto_fix.parse_review_output(
                output, finding_kind_aliases={
                    "target_alone": "correctness",
                    "peer_only": "correctness",
                })
            if shared_path_recovery is not None:
                if (record.verdict != "PASS" or record.findings
                        or record.shared_paths is None):
                    raise AssentError(
                        "shared-input recovery requires PASS, no findings, and "
                        "one non-null shared_paths decision")
                recovery_cfg, _contract, required, _failure, _preparation = (
                    shared_path_recovery)
                decision = record.shared_paths
                main = gitops.main_worktree(recovery_cfg.root)
                declared, _watched = shared_paths.validate_review_decision(
                    main, recovery_cfg.root,
                    decision.paths, decision.watch)
                omitted = sorted(set(required) - set(declared))
                if omitted:
                    raise AssentError(
                        "shared-input recovery omitted verifier-required "
                        "path(s): " + ", ".join(omitted))
            else:
                allowed_verdicts = (
                    {"FAIL", "FIXED"} if step.writes else {"FAIL"})
                if record.verdict not in allowed_verdicts or not record.findings:
                    raise AssentError(
                        "a failed selection verifier requires an allowed "
                        "non-PASS verdict with at least one finding")
            return record, prompt_digest, session
        except AssentError as error:
            if invalid_attempts >= work_configs[0].retry_per_task:
                raise
            invalid_attempts += 1
            diagnostic = _bounded_adapter_diagnostic(output)
            attempt_prompt = prompt + _auto_fix_review_correction(
                str(error), diagnostic)


def _selection_owned_findings(
        record: auto_fix.ReviewRecord,
        work_configs: tuple[Config, ...]) -> dict[str, auto_fix.ReviewRecord]:
    """Resolve each unchanged finding contract to exactly one selected task."""
    plans = {cfg.tasks_name: Plan.parse(cfg.tasks_dir) for cfg in work_configs}
    grouped: dict[str, list[auto_fix.ReviewFinding]] = {}
    for finding in record.findings:
        if finding.task_id is None:
            raise AssentError(
                f"selection finding {finding.path!r} names no existing task")
        candidates: list[tuple[str, auto_fix.ReviewFinding]] = []
        for cfg in work_configs:
            try:
                resolved = auto_fix.validate_review_findings(
                    auto_fix.ReviewRecord("FAIL", (finding,)),
                    plans[cfg.tasks_name])
            except AssentError:
                continue
            candidates.append((cfg.tasks_name, resolved.findings[0]))
        if len(candidates) != 1:
            shown = ", ".join(folder for folder, _item in candidates) or "none"
            raise AssentError(
                f"selection finding {finding.task_id} {finding.path!r} must "
                f"resolve to exactly one selected plan (matched: {shown})")
        folder, resolved = candidates[0]
        grouped.setdefault(folder, []).append(resolved)
    return {
        folder: auto_fix.ReviewRecord(record.verdict, tuple(findings))
        for folder, findings in grouped.items()
    }


def _selection_conflicts(
        state: SelectionWorkflowState) -> tuple[SelectionCandidateConflict, ...]:
    conflicts = selection_conflicts_from_evidence(state.action_evidence)
    if state.action_evidence and state.action_evidence[0] in {
            "TARGET_CONFLICT", "PEER_CONFLICT"} and not conflicts:
        raise AssentError(
            "selection candidate conflict action has no typed conflict wave")
    return conflicts


def _validate_selection_conflict_assignments(
        grouped: dict[str, auto_fix.ReviewRecord],
        conflicts: tuple[SelectionCandidateConflict, ...]) -> None:
    """Require the reviewer to map every conflict path and nothing unrelated."""
    if not conflicts:
        return
    allowed = {
        (conflict.folder, path)
        for conflict in conflicts for path in conflict.paths
    }
    assigned = {
        (folder, finding.path)
        for folder, record in grouped.items() for finding in record.findings
    }
    missing = sorted(allowed - assigned)
    extra = sorted(assigned - allowed)
    if missing or extra:
        detail: list[str] = []
        if missing:
            detail.append("unassigned: " + ", ".join(
                f"{folder}:{path}" for folder, path in missing))
        if extra:
            detail.append("outside conflict wave: " + ", ".join(
                f"{folder}:{path}" for folder, path in extra))
        raise AssentError(
            "selection conflict ownership is incomplete (" + "; ".join(detail)
            + ")")


def _git_blob_text(root, object_id: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{object_id}:{path}"], cwd=str(root),
        capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return "(path absent)"
    return _selection_bounded(result.stdout)


def _selection_three_way_evidence(
        root, conflict: SelectionCandidateConflict, *, target_reconcile: bool
        ) -> str:
    base = gitops.merge_base(root, conflict.target_tip, conflict.source_tip)
    ours = conflict.source_tip if target_reconcile else conflict.prefix_tree
    theirs = conflict.target_tip if target_reconcile else conflict.source_tip
    sections: list[str] = []
    for path in conflict.paths:
        sections.append(
            f"### {path}\nBASE ({base}):\n{_git_blob_text(root, base, path)}\n"
            f"OURS ({ours}):\n{_git_blob_text(root, ours, path)}\n"
            f"THEIRS ({theirs}):\n{_git_blob_text(root, theirs, path)}")
    return _selection_bounded("\n\n".join(sections))


def _selection_peer_context(
        cfg: Config, conflicts: tuple[SelectionCandidateConflict, ...]) -> str:
    relevant = [item for item in conflicts
                if item.folder == cfg.tasks_name and item.kind == "peer_only"]
    if not relevant:
        return ""
    parts = ["\n\nSelection peer-conflict context (read-only evidence):"]
    for conflict in relevant:
        prefix = ", ".join(
            f"{folder}@{tip}" for folder, tip in conflict.prefix_sources)
        parts.append(
            f"\nFolder: {conflict.folder}\nCompatible prefix: "
            f"{prefix or '(target only)'}\nCompatible prefix tree: "
            f"{conflict.prefix_tree}\nDependent exclusions: "
            + (", ".join(conflict.dependent_exclusions) or "none") + "\n"
            + _selection_three_way_evidence(
                cfg.root, conflict, target_reconcile=False))
    parts.append(
        "\nModify only this task's declared source scope into a form that "
        "merges with the complete exact selection. Do not accept a prefix, "
        "change the integration target, or create an implicit base.")
    return _selection_bounded("\n".join(parts))


def _persist_selection_findings(
        work_configs: tuple[Config, ...], selection: SelectionWorkflowState,
        step: WorkflowPlanStep, session: SessionIdentity,
        record: auto_fix.ReviewRecord,
        prompt_digest: str) -> dict[str, auto_fix.AutoFixState]:
    """Persist every plan assignment before selection repair can begin."""
    grouped = _selection_owned_findings(record, work_configs)
    _validate_selection_conflict_assignments(
        grouped, _selection_conflicts(selection))
    states: dict[str, auto_fix.AutoFixState] = {}
    blocker = "\n".join(selection.action_evidence)
    focused = "Selection full_verify ran only after every selected plan's " \
              "focused closeout passed."
    for cfg in work_configs:
        subset = grouped.get(cfg.tasks_name)
        if subset is None:
            continue
        plan = Plan.parse(cfg.tasks_dir)
        previous = None
        path = auto_fix.auto_fix_state_path(cfg)
        if path.exists():
            candidate = auto_fix.read_auto_fix_state(path)
            if candidate.review_context == "selection_verification":
                previous = candidate
        stage = "recheck" if previous is not None else "initial"
        subset = auto_fix.validate_review_transitions(
            subset, review_stage=stage, previous=previous,
            repair_changed_paths=(
                _auto_fix_changed_paths(cfg, previous.source_tree)
                if previous is not None else None))
        contracts_by_id = _task_contract_snapshots(plan)
        next_state = auto_fix.state_for_review(
            subset, source_tree=selection.action_candidate_tree,
            task_plan_sha256=_contracts_digest(plan, contracts_by_id),
            review_prompt_sha256=prompt_digest,
            reviewer_role=step.role, reviewer_step_index=selection.step_index,
            reviewer_adapter=session.agent,
            reviewer_model=session.requested_model,
            reviewer_effort=effort_identity(session.requested_effort),
            previous=previous, review_context="selection_verification",
            review_stage=stage, workflow_step_index=selection.step_index + 1,
            enforce_transitions=False)
        next_state = _auto_fix_attach_repair_briefs(
            cfg, plan, next_state, blocker_evidence=blocker,
            focused_evidence=focused)
        auto_fix.write_auto_fix_state(path, next_state)
        states[cfg.tasks_name] = next_state
    return states


def _selection_repair_states(
        work_configs: tuple[Config, ...]) -> dict[str, auto_fix.AutoFixState]:
    states: dict[str, auto_fix.AutoFixState] = {}
    for cfg in work_configs:
        path = auto_fix.auto_fix_state_path(cfg)
        if not path.exists():
            continue
        state = auto_fix.read_auto_fix_state(path)
        if (state.review_context == "selection_verification"
                and state.verdict != "PASS"):
            states[cfg.tasks_name] = state
    if not states:
        raise AssentError("selection repair has no durable assigned findings")
    return states


def _selection_findings_already_persisted(
        work_configs: tuple[Config, ...],
        selection: SelectionWorkflowState) -> bool:
    """Recognize the crash boundary after findings but before cursor advance."""
    found = False
    for cfg in work_configs:
        path = auto_fix.auto_fix_state_path(cfg)
        if not path.exists():
            continue
        state = auto_fix.read_auto_fix_state(path)
        if state.review_context != "selection_verification":
            continue
        if (state.reviewer_step_index == selection.step_index
                and state.source_tree == selection.action_candidate_tree
                and state.verdict == "FAIL"):
            found = True
    return found


def _selection_merged_fix_already_persisted(
        work_configs: tuple[Config, ...],
        selection: SelectionWorkflowState,
        conflicts: tuple[SelectionCandidateConflict, ...]) -> bool:
    """Recognize the post-merge, post-ledger focused-closeout boundary."""
    expected = {conflict.folder for conflict in conflicts}
    found: set[str] = set()
    for cfg in work_configs:
        path = auto_fix.auto_fix_state_path(cfg)
        if not path.exists():
            continue
        state = auto_fix.read_auto_fix_state(path)
        if (state.review_context == "selection_verification"
                and state.reviewer_step_index == selection.step_index
                and state.source_tree == selection.action_candidate_tree
                and state.verdict == "FIXED"):
            found.add(cfg.tasks_name)
    return bool(expected) and expected <= found


def _selection_assignment_detail(
        selection: SelectionWorkflowState, task: Task,
        state: auto_fix.AutoFixState) -> str:
    return (
        "The selection-verification finding ledger and repair brief were "
        "persisted before any write-capable session in this repair wave.\n"
        f"selection candidate: {selection.action_candidate_tree}\n"
        f"task: {task.id}\n"
        "finding fingerprints: "
        + json.dumps(list(state.current_finding_fingerprints),
                     separators=(",", ":")))


def _selection_assignment_recorded(task: Task, detail: str) -> bool:
    return any(
        entry.get("by") == "scheduler"
        and entry.get("event") == "auto_fix_attempt"
        and entry.get("detail") in {detail, detail + "\n"}
        for entry in read_entries(task.journal_path))


def _mark_selection_reviews_passed(
        work_configs: tuple[Config, ...], candidate_tree: str,
        workflow_step_index: int) -> None:
    """Close durable repair ledgers only after the real receipt passes."""
    for cfg in work_configs:
        path = auto_fix.auto_fix_state_path(cfg)
        if not path.exists():
            continue
        previous = auto_fix.read_auto_fix_state(path)
        if (previous.review_context != "selection_verification"
                or previous.verdict == "PASS"):
            continue
        plan = Plan.parse(cfg.tasks_dir)
        passed = auto_fix.state_for_review(
            auto_fix.ReviewRecord("PASS", ()), previous=previous,
            source_tree=candidate_tree,
            task_plan_sha256=_contracts_digest(
                plan, _task_contract_snapshots(plan)),
            review_prompt_sha256=previous.review_prompt_sha256,
            reviewer_role=previous.reviewer_role,
            reviewer_step_index=previous.reviewer_step_index,
            reviewer_adapter=previous.reviewer_adapter,
            reviewer_model=previous.reviewer_model,
            reviewer_effort=previous.reviewer_effort,
            review_context="selection_verification", review_stage="recheck",
            workflow_step_index=workflow_step_index)
        auto_fix.write_auto_fix_state(path, passed)


def _selection_fixer_sessions(
        cfg: Config, task: Task, step: WorkflowPlanStep
        ) -> tuple[_AdapterRotation, dict[str, SessionIdentity]]:
    """Resolve every declared adapter candidate for a selection fixer role."""
    rotation = _workflow_step_rotation(cfg, step)
    plan = Plan.parse(cfg.tasks_dir)
    sessions: dict[str, SessionIdentity] = {}
    for adapter_name, adapter in zip(rotation.names, rotation.adapters):
        session = _plan_step_session(cfg, adapter, plan, step, adapter_name)
        errors = adapter.preflight([InvocationRequest(
            task_id=f"{cfg.tasks_name} selection conflict fixer",
            model=step.model or task.model, effort=session.effort,
            requested_model=session.requested_model,
            requested_effort=session.requested_effort)])
        if errors:
            raise AssentError(
                f"selection conflict fixer capability unavailable for "
                f"{cfg.tasks_name}/{adapter_name}: " + "; ".join(errors))
        sessions[adapter_name] = session
    return rotation, sessions


def _workflow_fixer_profile(
        cfg: Config, task: Task, step: WorkflowPlanStep) -> _FixerProfile:
    model = step.model or task.model
    effort = (
        step.effort
        if step.model is not None and step.produces_verdict
        else inherited_effort(step.model, step.effort, task.effort))
    return _FixerProfile(step.adapters, model, effort)


def _selection_reconcile_prompt(
        cfg: Config, conflict: SelectionCandidateConflict,
        step: WorkflowPlanStep, worktree) -> str:
    plan = Plan.parse(cfg.tasks_dir)
    contracts: list[str] = []
    for task in plan.tasks:
        try:
            text = task.path.read_text(encoding="utf-8")
        except OSError as error:
            raise AssentError(
                f"unable to read reconcile contract {task.id}: {error}") from error
        contracts.append(f"--- {task.id}: {task.path} ---\n{text.rstrip()}")
    return _SELECTION_RECONCILE_PROMPT.format(
        agents_md_path=_agents_md_absolute_path_for_prompt(cfg),
        instructions_path=contracts.instructions_path(),
        workflow_role=step.role,
        role_policy="\n\n".join(
            ability.prompt for ability in step.resolved_role.abilities),
        folder=cfg.tasks_name, worktree=worktree,
        source_tip=conflict.source_tip, target_tip=conflict.target_tip,
        conflict_paths="\n".join(f"- {path}" for path in conflict.paths),
        three_way_evidence=_selection_three_way_evidence(
            cfg.root, conflict, target_reconcile=True),
        contracts=_selection_bounded("\n\n".join(contracts)))


def _selection_transition_recorded(task: Task, detail: str) -> bool:
    return any(
        entry.get("by") == "scheduler"
        and entry.get("event") == "selection_source_transition"
        and entry.get("detail") in {detail, detail + "\n"}
        for entry in read_entries(task.journal_path))


def _run_selection_target_reconciles(
        work_configs: tuple[Config, ...],
        conflicts: tuple[SelectionCandidateConflict, ...],
        fixer_step: WorkflowPlanStep, *, sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> None:
    """Resolve every target-alone member of one reviewed conflict wave."""
    configs = {cfg.tasks_name: cfg for cfg in work_configs}
    for conflict in conflicts:
        if conflict.kind != "target_alone":
            continue
        cfg = configs.get(conflict.folder)
        if cfg is None:
            raise AssentError(
                f"selection conflict names unknown folder {conflict.folder}")
        plan = Plan.parse(cfg.tasks_dir)
        review_record = auto_fix.current_review_record(
            _selection_repair_states(work_configs)[cfg.tasks_name])
        owner_ids = {
            finding.task_id for finding in review_record.findings
            if finding.path in conflict.paths and finding.task_id is not None
        }
        owner = next((task for task in plan.tasks
                      if task.id in owner_ids), None)
        if owner is None:
            raise AssentError(
                f"target conflict in {cfg.tasks_name} has no reviewed task owner")
        rotation, sessions = _selection_fixer_sessions(
            cfg, owner, fixer_step)
        context = reconcile.automatic_reconcile_prepare_locked(
            cfg, conflict.target_tip, conflict.source_tip, conflict.paths)
        if context.needs_editing:
            assert context.worktree is not None
            prompt = _selection_reconcile_prompt(
                cfg, conflict, fixer_step, context.worktree)
            baselines = tuple(
                _selection_surface_baseline(item) for item in work_configs)
            failed_adapters: set[str] = set()
            while True:
                adapter_name = rotation.name
                adapter = rotation.adapter
                session = sessions[adapter_name]
                print(f"Selection reconcile session: {adapter_name} | "
                      f"{fixer_step.model or owner.model}->"
                      f"{session.requested_model} | "
                      f"{effort_identity(session.effort)}->"
                      f"{effort_identity(session.requested_effort)}")
                result = _invoke_adapter(
                    cfg, adapter, adapter_name, prompt,
                    session.requested_model, session.requested_effort,
                    context.worktree, context_kind="selection",
                    context_id="workflow.selection.reconcile",
                    folders=tuple(item.tasks_name for item in work_configs))
                changed = _selection_surface_changes(
                    work_configs, baselines)
                if changed:
                    raise AssentError(
                        "selection reconcile fixer wrote protected source or "
                        "management paths; exact edits were preserved: "
                        + ", ".join(changed[:8]))
                if (result.checkpoint_resume and not result.quota_exhausted
                        and not result.stalled and result.exit_code != 0):
                    continue
                if result.quota_exhausted:
                    if len(rotation.names) == 1:
                        _wait_for_quota(cfg, result.reset_at, sleep, now)
                    elif rotation.advance_after_quota(failed_adapters):
                        _wait_for_rotation(cfg, sleep)
                    continue
                if result.failure_kind == "authentication":
                    reason = _adapter_failure_reason(
                        result.exit_code, result.stalled, result.output,
                        result.failure_kind)
                    action = _authentication_failover_action(
                        rotation, failed_adapters, reason)
                    if action == "switch":
                        print("Selection reconcile authentication failure; "
                              f"switching {adapter_name} -> {rotation.name}.")
                    else:
                        print("Selection reconcile authentication failure; "
                              "waiting only for recoverable adapter "
                              f"{rotation.name}.")
                        _wait_for_rotation(cfg, sleep)
                    continue
                if result.exit_code != 0 or result.stalled:
                    if _adapter_availability_failed(result):
                        switched = rotation.advance_after_failure(
                            failed_adapters)
                        if switched:
                            print("Selection reconcile adapter failure; switching "
                                  f"{adapter_name} -> {rotation.name}.")
                        else:
                            print("Selection reconcile adapters unavailable; "
                                  f"waiting {cfg.rotation_poll_minutes} minute(s) "
                                  f"before restarting with {rotation.name}.")
                            _wait_for_rotation(cfg, sleep)
                        continue
                    raise AssentError(
                        "selection reconcile fixer adapter failure: "
                        + _adapter_failure_reason(
                            result.exit_code, result.stalled, result.output,
                            result.failure_kind))
                break
        merge_commit = reconcile.automatic_reconcile_continue_locked(
            cfg, conflict.target_tip, conflict.source_tip, conflict.paths)
        detail = (
            f"selection target: {conflict.target_tip}\n"
            f"source before: {conflict.source_tip}\n"
            f"source after: {merge_commit}\n"
            "conflict paths: " + json.dumps(
                list(conflict.paths), separators=(",", ":")))
        for task in plan.tasks:
            if task.id not in owner_ids:
                continue
            if not _selection_transition_recorded(task, detail):
                append_entry(
                    task.journal_path, by="scheduler",
                    event="selection_source_transition",
                    summary=("Automatic selection reconciliation advanced "
                             f"{cfg.tasks_name} without changing the target"),
                    detail=detail, agent=session.agent,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort,
                    time_str=now().isoformat(timespec="seconds"))


def _selection_merged_workspace_text(
        work_configs: tuple[Config, ...],
        conflicts: tuple[SelectionCandidateConflict, ...],
        contexts: dict[str, reconcile.AutomaticReconcile]) -> str:
    """Describe every scheduler-owned path one merged selection role may edit."""
    target_folders = {
        conflict.folder for conflict in conflicts
        if conflict.kind == "target_alone"
    }
    lines: list[str] = []
    for cfg in work_configs:
        mode = ("READ ONLY: use the managed reconcile worktree below"
                if cfg.tasks_name in target_folders else
                "WRITABLE within the listed task scopes")
        lines.append(
            f"## {cfg.tasks_name} source worktree\nPath: {cfg.root}\n{mode}")
        for task in Plan.parse(cfg.tasks_dir).tasks:
            lines.append(
                f"- {task.id} scopes: " + ", ".join(task.scope))
    for conflict in conflicts:
        if conflict.kind == "target_alone":
            context = contexts[conflict.folder]
            location = (str(context.worktree) if context.worktree is not None
                        else "already reconciled by an earlier resumed step")
            mode = "managed reconcile worktree"
        else:
            cfg = next(item for item in work_configs
                       if item.tasks_name == conflict.folder)
            location = str(cfg.root)
            mode = "source worktree"
        lines.append(
            f"## {conflict.folder} {mode}\nPath: {location}\n"
            "Exact conflict paths:\n"
            + "\n".join(f"- {path}" for path in conflict.paths)
            + "\nThree-way evidence:\n"
            + _selection_three_way_evidence(
                work_configs[0].root, conflict,
                target_reconcile=conflict.kind == "target_alone"))
    return _selection_bounded("\n\n".join(lines))


def _checkpoint_selection_merged_writes(
        work_configs: tuple[Config, ...]) -> None:
    """Keep interrupted merged-session source output in recoverable WIP commits."""
    for cfg in work_configs:
        if gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
            continue
        gitops.commit_if_dirty(
            cfg.root,
            f"wip({cfg.tasks_name}): interrupted selection reviewer-fixer",
            cfg.git_excludes)


_INTEGRATION_UNRESOLVED = 2


def _integration_unresolved(reason: str, *, nested: bool = False) -> int:
    """End finite integration automation without misreporting it as a crash."""
    print("Integration workflow: REVIEW UNRESOLVED, HUMAN DECISION; " + reason
          + ". Evidence and edits were preserved. `assent accept` remains "
            "blocked until full_verify passes.")
    return _INTEGRATION_UNRESOLVED if nested else 0


def _selection_reconcile_path_state(
        root: Path, paths: tuple[str, ...]) -> tuple[tuple[str, str, str], ...]:
    """Bind whether the merged session actually edited conflict-path content."""
    result: list[tuple[str, str, str]] = []
    for relative in paths:
        path = root / relative
        if path.is_symlink():
            result.append((relative, "link", os.readlink(path)))
        elif path.is_file():
            result.append((
                relative, "file",
                sha256_file(path, "selection conflict path")))
        elif path.exists():
            result.append((relative, "other", ""))
        else:
            result.append((relative, "missing", ""))
    return tuple(result)


def _apply_selection_shared_path_review(
        cfg: Config, state: SelectionWorkflowState, step: WorkflowPlanStep,
        contract: "shared_paths.Contract", required: tuple[str, ...],
        evidence: str, *, preparation: bool,
        sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> None:
    """Run and apply one scheduler-owned, read-only shared-path decision."""
    main = gitops.main_worktree(cfg.root)
    record, _prompt_digest, _session = _run_selection_reviewer(
        (cfg,), state, step, sleep=sleep, now=now,
        shared_path_recovery=(
            cfg, contract, required, evidence, preparation))
    decision = record.shared_paths
    assert decision is not None
    declared, watched = shared_paths.validate_review_decision(
        main, cfg.root, decision.paths, decision.watch)
    omitted = sorted(set(required) - set(declared))
    if omitted:
        raise AssentError(
            "shared-input recovery omitted verifier-required path(s): "
            + ", ".join(omitted))
    shared_paths.review(
        main, cfg.root, paths=declared, watch=watched,
        none=not declared)
    settled = shared_paths.classify(
        main, cfg.root, required_evidence=required)
    if not settled.settled:
        raise AssentError(
            "shared-input recovery did not settle the active profile")
    shared_paths.require_directory_link_agreement(
        main, cfg.root, settled, folder=cfg.tasks_name)


def _recover_pending_selection_shared_paths(
        work_configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: WorkflowPlanStep, *, sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> None:
    """Settle worktree-preparation evidence before any focused closeout."""
    for cfg in work_configs:
        contract = _shared_paths_contract(cfg)
        if not contract.needs_review:
            continue
        existing = set(shared_paths.ignored_directory_links(cfg.root))
        required = tuple(sorted(existing - set(contract.paths)))
        evidence = "\n".join(contract.evidence) or (
            "The source snapshot has no reusable shared-path profile.")
        print("Integration preparation: reviewing the stale shared-path "
              f"contract for {cfg.tasks_name} before focused checks.")
        _apply_selection_shared_path_review(
            cfg, state, step, contract, required, evidence,
            preparation=True, sleep=sleep, now=now)


def _recover_selection_focused_shared_paths(
        cfg: Config, state: SelectionWorkflowState, step: WorkflowPlanStep,
        failure_output: str, *, sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> bool:
    """Review one active profile contradicted by concrete focused output."""
    main = gitops.main_worktree(cfg.root)
    required = verification.mentioned_ordinary_ignored_directories(
        failure_output, (main,))
    if not required:
        return False
    contract = shared_paths.classify(
        main, cfg.root, required_evidence=required)
    if not contract.needs_review:
        return False
    print("Integration focused_sweep: the active shared-path profile omitted "
          "verifier-named input(s): " + ", ".join(required))
    _apply_selection_shared_path_review(
        cfg, state, step, contract, required, failure_output,
        preparation=False, sleep=sleep, now=now)
    print("Integration focused_sweep: shared-path decision applied; retrying "
          "the same focused checks.")
    return True


def _closeout_selection_merged_repairs_locked(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        next_action_index: int,
        repair_states: dict[str, auto_fix.AutoFixState],
        step: WorkflowPlanStep, *, sleep: Callable[[float], None],
        now: Callable[[], datetime],
        ) -> SelectionWorkflowState | None:
    for cfg in configs:
        review_state = repair_states.get(cfg.tasks_name)
        if review_state is not None:
            review_state = auto_fix.with_repair_phase(
                review_state, "AWAITING_REVIEW")
            auto_fix.write_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg), review_state)
        if not any(task.status == "DONE"
                   for task in Plan.parse(cfg.tasks_dir).tasks):
            continue
        failure_output: list[str] = []
        if _verify_focused_locked(
                cfg, failure_output=failure_output) != 0:
            if (not failure_output
                    or not _recover_selection_focused_shared_paths(
                        cfg, state, step, "\n".join(failure_output),
                        sleep=sleep, now=now)
                    or _verify_focused_locked(cfg) != 0):
                return None
        gitops.ensure_clean(cfg.root, cfg.git_excludes)

    target_ref, target_commit, source_commits = _selection_snapshot(configs)
    if target_ref != state.target_ref or target_commit != state.target_commit:
        raise AssentError(
            "selection target changed during merged repair closeout")
    state = replace(
        state, source_commits=source_commits,
        step_index=next_action_index, action_status="STALE",
        repair_phase="RECHECK")
    write_selection_workflow_state(configs[0].assent_dir, state)
    return state


def _run_selection_merged_repairs(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: WorkflowPlanStep, *, sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> tuple[int, SelectionWorkflowState]:
    """Let one writable verdict session review and repair a failed selection."""
    next_action_index = state.step_index + 1
    steps = configs[0].workflow_integration
    if (next_action_index >= len(steps)
            or not isinstance(steps[next_action_index], WorkflowActionStep)):
        return _integration_unresolved(
            "the configured steps ended without a final full_verify",
            nested=True), state

    work_configs = _selection_worktree_configs(configs)
    conflicts = _selection_conflicts(state)
    original_sources = dict(zip(state.folders, state.source_commits))
    target_folders = {
        conflict.folder for conflict in conflicts
        if conflict.kind == "target_alone"
    }
    cfg_by_folder = {cfg.tasks_name: cfg for cfg in work_configs}

    with _selection_locks(configs):
        current_target_ref = gitops.require_current_branch(
            gitops.main_worktree(configs[0].root))
        current_target = gitops.commit_of(
            gitops.main_worktree(configs[0].root), current_target_ref)
        if (current_target_ref != state.target_ref
                or current_target != state.target_commit):
            raise AssentError(
                "selection target changed while merged repair was pending")
        if state.repair_phase == "NONE":
            state = replace(state, repair_phase="MERGED")
            write_selection_workflow_state(configs[0].assent_dir, state)

        _recover_pending_selection_shared_paths(
            work_configs, state, step, sleep=sleep, now=now)

        contexts: dict[str, reconcile.AutomaticReconcile] = {}
        reconcile_baselines: dict[str, tuple[tuple[str, str, str], ...]] = {}
        preexisting_reconcile_repairs: set[str] = set()
        for conflict in conflicts:
            if conflict.kind != "target_alone":
                continue
            cfg = cfg_by_folder.get(conflict.folder)
            if cfg is None:
                raise AssentError(
                    f"selection conflict names unknown folder {conflict.folder}")
            context = reconcile.automatic_reconcile_prepare_locked(
                cfg, conflict.target_tip, conflict.source_tip, conflict.paths)
            contexts[conflict.folder] = context
            if context.worktree is not None and context.needs_editing:
                reconcile_baselines[conflict.folder] = (
                    _selection_reconcile_path_state(
                        context.worktree, conflict.paths))
                if not gitops.merge_scene_is_unedited(
                        context.worktree, conflict.source_tip,
                        conflict.target_tip):
                    preexisting_reconcile_repairs.add(conflict.folder)

        if _selection_merged_fix_already_persisted(
                work_configs, state, conflicts):
            if any(context.needs_editing for context in contexts.values()):
                raise AssentError(
                    "persisted selection repair precedes its source transition")
            resumed = _closeout_selection_merged_repairs_locked(
                work_configs, state, next_action_index,
                _selection_repair_states(work_configs), step,
                sleep=sleep, now=now)
            return (0, resumed) if resumed is not None else (1, state)

        workspaces = _selection_merged_workspace_text(
            work_configs, conflicts, contexts)
        try:
            record, prompt_digest, review_session = _run_selection_reviewer(
                work_configs, state, step, sleep=sleep, now=now,
                repair_workspaces=workspaces)
            grouped = _selection_owned_findings(record, work_configs)
            _validate_selection_conflict_assignments(grouped, conflicts)
            scope_additions: dict[
                str, tuple[auto_fix.ApprovedScopeAddition, ...]] = {}
            for cfg in work_configs:
                subset = grouped.get(cfg.tasks_name)
                if subset is None:
                    continue
                additions = tuple(
                    auto_fix.ApprovedScopeAddition(
                        auto_fix.finding_fingerprint(finding), finding.task_id,
                        finding.scope_addition.path,
                        finding.scope_addition.path_state)
                    for finding in subset.findings
                    if finding.task_id is not None
                    and finding.scope_addition is not None)
                if not additions:
                    continue
                if record.verdict != "FIXED":
                    raise AssentError(
                        "A writable integration verdict role must repair an "
                        "approved scope omission in the same session and return FIXED")
                if cfg.tasks_name in target_folders:
                    raise AssentError(
                        "A target-conflict reconciliation cannot also amend task "
                        "scope in the same repair transaction")
                plan = Plan.parse(cfg.tasks_dir)
                auto_fix.validate_scope_additions(
                    cfg.root, plan, additions,
                    baseline_ref=original_sources[cfg.tasks_name],
                    materialized_new_files=True)
                changed_paths = {
                    path.replace("\\", "/")
                    for path in gitops.dirty_paths(cfg.root, cfg.git_excludes)
                }
                missing = [item.path for item in additions
                           if item.path not in changed_paths]
                if missing:
                    raise AssentError(
                        "A same-session integration scope amendment did not repair "
                        "its exact path: " + ", ".join(missing))
                scope_additions[cfg.tasks_name] = additions

            session_changed = False
            repair_present = bool(preexisting_reconcile_repairs)
            for cfg in work_configs:
                old_source = original_sources[cfg.tasks_name]
                source_changed = (
                    gitops.commit_of(cfg.root, "HEAD") != old_source
                    or not gitops.working_tree_status(
                        cfg.root, cfg.git_excludes).is_clean)
                if cfg.tasks_name in target_folders:
                    context = contexts[cfg.tasks_name]
                    if context.needs_editing and source_changed:
                        raise AssentError(
                            "a merged selection role edited the source worktree "
                            f"for target-conflict folder {cfg.tasks_name}; it "
                            "must edit only the managed reconcile worktree")
                    reconcile_changed = False
                    if context.worktree is not None and context.needs_editing:
                        reconcile_changed = (
                            _selection_reconcile_path_state(
                                context.worktree, context.conflict_paths)
                            != reconcile_baselines[cfg.tasks_name])
                    session_changed = (
                        session_changed or reconcile_changed or source_changed)
                    repair_present = (
                        repair_present or reconcile_changed or source_changed)
                    continue
                subset = grouped.get(cfg.tasks_name)
                scopes: list[str] = []
                if subset is not None:
                    plan = Plan.parse(cfg.tasks_dir)
                    for finding in subset.findings:
                        owner = plan.get(finding.task_id) if finding.task_id else None
                        if owner is not None:
                            scopes.extend(owner.scope)
                scopes.extend(
                    item.path for item in scope_additions.get(
                        cfg.tasks_name, ()))
                outside = gitops.changes_outside_scope(
                    cfg.root, list(dict.fromkeys(scopes)),
                    since_ref=old_source, excludes=cfg.git_excludes)
                if outside:
                    raise AssentError(
                        "merged selection reviewer-fixer wrote outside the "
                        f"scopes named by its findings in {cfg.tasks_name}: "
                        + ", ".join(outside[:8]))
                session_changed = session_changed or source_changed
                repair_present = repair_present or source_changed

            if record.verdict != "FIXED":
                if session_changed:
                    raise AssentError(
                        "a writable selection role returned FAIL after changing "
                        "a repair workspace")
                _persist_selection_findings(
                    work_configs, state, step, review_session, record,
                    prompt_digest)
                return _integration_unresolved(
                    "the final reviewer-fixer reported unresolved findings "
                    "without a repair", nested=True), state
            if not repair_present:
                raise AssentError(
                    "selection reviewer-fixer returned FIXED without changing "
                    "any repair workspace")

            session = review_session
            for conflict in conflicts:
                if conflict.kind != "target_alone":
                    continue
                cfg = cfg_by_folder[conflict.folder]
                subset = grouped.get(conflict.folder)
                assert subset is not None
                owner_ids = {
                    finding.task_id for finding in subset.findings
                    if finding.path in conflict.paths
                    and finding.task_id is not None
                }
                merge_commit = reconcile.automatic_reconcile_continue_locked(
                    cfg, conflict.target_tip, conflict.source_tip,
                    conflict.paths)
                detail = (
                    f"selection target: {conflict.target_tip}\n"
                    f"source before: {conflict.source_tip}\n"
                    f"source after: {merge_commit}\n"
                    "conflict paths: " + json.dumps(
                        list(conflict.paths), separators=(",", ":")))
                for task in Plan.parse(cfg.tasks_dir).tasks:
                    if task.id not in owner_ids:
                        continue
                    if not _selection_transition_recorded(task, detail):
                        append_entry(
                            task.journal_path, by="scheduler",
                            event="selection_source_transition",
                            summary=("Merged selection reviewer-fixer resolved "
                                     "the target conflict without changing "
                                     "the integration target"),
                            detail=detail, agent=session.agent,
                            requested_model=session.requested_model,
                            requested_effort=session.requested_effort,
                            time_str=now().isoformat(timespec="seconds"))

            for cfg in work_configs:
                if cfg.tasks_name in target_folders:
                    continue
                gitops.commit_if_dirty(
                    cfg.root,
                    f"auto({cfg.tasks_name}): selection reviewer-fixer repair",
                    cfg.git_excludes)

            repair_states = _persist_selection_findings(
                work_configs, state, step, review_session, record,
                prompt_digest)
            for cfg in work_configs:
                review_state = repair_states.get(cfg.tasks_name)
                if (review_state is None
                        or cfg.tasks_name not in scope_additions):
                    continue
                plan = Plan.parse(cfg.tasks_dir)
                review_state, _plan, _contracts = (
                    _apply_reviewed_scope_amendments(
                        cfg, review_state, plan,
                        _task_contract_snapshots(plan), now,
                        baseline_ref=original_sources[cfg.tasks_name],
                        materialized_new_files=True))
                repair_states[cfg.tasks_name] = review_state
        except BaseException:
            _checkpoint_selection_merged_writes(work_configs)
            raise

        _recover_pending_selection_shared_paths(
            work_configs, state, step, sleep=sleep, now=now)
        closed = _closeout_selection_merged_repairs_locked(
            work_configs, state, next_action_index, repair_states, step,
            sleep=sleep, now=now)
        if closed is None:
            return 1, state
        state = closed
    return 0, state


def _run_selection_repairs(
        configs: tuple[Config, ...], state: SelectionWorkflowState,
        step: WorkflowPlanStep, *, sleep: Callable[[float], None],
        now: Callable[[], datetime]) -> tuple[int, SelectionWorkflowState]:
    """Resume one coalesced selection finding wave through its focused sweep."""
    work_configs = _selection_worktree_configs(configs)
    conflicts = _selection_conflicts(state)
    fixer_step: WorkflowPlanStep | None = None
    next_action_index = state.step_index + 1
    if conflicts:
        if (next_action_index < len(configs[0].workflow_integration)
                and isinstance(
                    configs[0].workflow_integration[next_action_index],
                    WorkflowPlanStep)):
            candidate = configs[0].workflow_integration[next_action_index]
            if candidate.writes and not candidate.produces_verdict:
                fixer_step = candidate
                next_action_index += 1
        if fixer_step is None:
            return _integration_unresolved(
                "a reviewed candidate conflict has no configured writable "
                "fixer step", nested=True), state
    elif (state.action_evidence
          and state.action_evidence[0] == "VERIFIER_FAILED"
          and next_action_index < len(configs[0].workflow_integration)):
        candidate = configs[0].workflow_integration[next_action_index]
        if (isinstance(candidate, WorkflowPlanStep)
                and candidate.writes and not candidate.produces_verdict):
            # This role exists for candidate conflicts.  A verifier failure is
            # repaired through the implicated tasks' own profiles, so the same
            # configured workflow proceeds directly to its objective gate.
            next_action_index += 1
    if (next_action_index >= len(configs[0].workflow_integration)
            or not isinstance(
                configs[0].workflow_integration[next_action_index],
                WorkflowActionStep)):
        return _integration_unresolved(
            "the configured repair wave has no following full_verify",
            nested=True), state
    if state.repair_phase == "NONE":
        if not _selection_findings_already_persisted(work_configs, state):
            record, prompt_digest, review_session = _run_selection_reviewer(
                work_configs, state, step, sleep=sleep, now=now)
            _persist_selection_findings(
                work_configs, state, step, review_session, record,
                prompt_digest)
        state = replace(state, repair_phase="NEEDS_REPAIR")
        write_selection_workflow_state(configs[0].assent_dir, state)

    repair_states = _selection_repair_states(work_configs)
    peer_folders = {
        conflict.folder for conflict in conflicts
        if conflict.kind == "peer_only"
    }
    assignments: list[tuple[Config, Task, _FixerProfile, _AdapterRotation,
                            dict[str, SessionIdentity]]] = []
    for cfg in work_configs:
        review_state = repair_states.get(cfg.tasks_name)
        if review_state is None:
            continue
        if conflicts and cfg.tasks_name not in peer_folders:
            continue
        plan = Plan.parse(cfg.tasks_dir)
        implicated = list(dict.fromkeys(
            item.task_id for item in review_state.findings
            if item.fingerprint in review_state.current_finding_fingerprints
            and item.task_id is not None))
        for task in _auto_fix_cascade_tasks(plan, implicated):
            profile = (_workflow_fixer_profile(cfg, task, fixer_step)
                       if fixer_step is not None
                       else _auto_fix_profile_for_task(cfg, task))
            fixer_rotation, sessions, errors = _fixer_rotation_and_sessions(
                cfg, profile)
            if errors:
                raise AssentError(
                    f"selection fixer capability unavailable for "
                    f"{cfg.tasks_name}/{task.id}: " + "; ".join(errors))
            assignments.append((cfg, task, profile, fixer_rotation, sessions))

    for cfg, task, profile, _fixer_rotation, sessions in assignments:
        session = sessions[profile.adapter]
        review_state = repair_states[cfg.tasks_name]
        detail = _selection_assignment_detail(state, task, review_state)
        if not _selection_assignment_recorded(task, detail):
            append_entry(
                task.journal_path, by="scheduler", event="auto_fix_attempt",
                summary=("Selection verification repair assignment: "
                         f"{profile.adapter}/{profile.model}/{profile.effort}"),
                detail=detail, agent=session.agent,
                requested_model=session.requested_model,
                requested_effort=session.requested_effort,
                time_str=now().isoformat(timespec="seconds"))

    with _selection_locks(configs):
        current_target_ref, current_target, _current_sources = (
            _selection_snapshot(configs))
        if (current_target_ref != state.target_ref
                or current_target != state.target_commit):
            raise AssentError(
                "selection target changed while verification repair was pending")
        entering_repair = state.repair_phase == "NEEDS_REPAIR"
        if entering_repair:
            for cfg in work_configs:
                review_state = repair_states.get(cfg.tasks_name)
                if review_state is None:
                    continue
                plan = Plan.parse(cfg.tasks_dir)
                review_state, _plan, _contracts = (
                    _apply_reviewed_scope_amendments(
                        cfg, review_state, plan,
                        _task_contract_snapshots(plan), now))
                review_state = auto_fix.with_repair_phase(
                    review_state, "REPAIRING")
                auto_fix.write_auto_fix_state(
                    auto_fix.auto_fix_state_path(cfg), review_state)
                repair_states[cfg.tasks_name] = review_state
            state = replace(state, repair_phase="REPAIRING")
            write_selection_workflow_state(configs[0].assent_dir, state)

        if conflicts:
            assert fixer_step is not None
            _run_selection_target_reconciles(
                work_configs, conflicts, fixer_step, sleep=sleep, now=now)

        if entering_repair:
            for cfg in work_configs:
                review_state = repair_states.get(cfg.tasks_name)
                if review_state is None:
                    continue
                if conflicts and cfg.tasks_name not in peer_folders:
                    continue
                implicated = list(dict.fromkeys(
                    item.task_id for item in review_state.findings
                    if item.fingerprint in review_state.current_finding_fingerprints
                    and item.task_id is not None))
                if rework.rework_tasks_locked(
                        cfg, implicated,
                        "Automatic repair of durable selection-verification "
                        "findings") != 0:
                    return 1, state

        active = _ActiveTask()
        dispositions: dict[str, list[auto_fix.WorkerDisposition]] = {}
        for cfg in work_configs:
            review_state = repair_states.get(cfg.tasks_name)
            if review_state is None:
                continue
            recovered = _auto_fix_recover_dispositions(
                review_state, Plan.parse(cfg.tasks_dir))
            if recovered != review_state:
                review_state = recovered
                auto_fix.write_auto_fix_state(
                    auto_fix.auto_fix_state_path(cfg), review_state)
                repair_states[cfg.tasks_name] = review_state
            dispositions[cfg.tasks_name] = list(
                review_state.worker_dispositions)

        for cfg, assigned, profile, fixer_rotation, sessions in assignments:
            task = Plan.parse(cfg.tasks_dir).get(assigned.id)
            if task is None or task.status in ("DONE", "SKIP"):
                continue
            if task.status not in ("TODO", "WIP"):
                print(f"Selection repair stopped: {cfg.tasks_name}/{task.id} "
                      f"is {task.status}, not TODO/WIP.")
                return 1, state
            review_state = repair_states[cfg.tasks_name]
            task_dispositions = dispositions[cfg.tasks_name]
            session_state = _SessionState()
            active.task = task
            active.session = session_state
            failure = _process_task(
                cfg, task, fixer_rotation,
                sleep, now, session_state, resumed=task.status == "WIP",
                session_overrides=sessions, profile_model=profile.model,
                auto_fix_context=(
                    _auto_fix_repair_context(task, review_state)
                    + _selection_peer_context(cfg, conflicts)),
                retry_limit=0, billing_is_failure=True,
                auto_fix_fingerprints=(
                    review_state.current_finding_fingerprints),
                repair_dispositions=task_dispositions,
                usage_context_kind="selection",
                usage_context_id=f"workflow.selection.repair:{task.id}",
                usage_folders=state.folders)
            active.task = None
            active.session = None
            updated = auto_fix.with_worker_dispositions(
                review_state, tuple(task_dispositions))
            auto_fix.write_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg), updated)
            repair_states[cfg.tasks_name] = updated
            if failure is not None:
                print(f"Selection repair focused closeout failed for "
                      f"{cfg.tasks_name}/{task.id}: {failure}")
                return 1, state

        for cfg in work_configs:
            review_state = repair_states.get(cfg.tasks_name)
            if review_state is None:
                continue
            incomplete = [
                f"{task.id}={task.status}" for task in Plan.parse(cfg.tasks_dir).tasks
                if task.status not in ("DONE", "SKIP")]
            if incomplete:
                print(f"Selection repair remains incomplete in {cfg.tasks_name}: "
                      + ", ".join(incomplete))
                return 1, state
            review_state = auto_fix.with_repair_phase(
                review_state, "AWAITING_REVIEW")
            auto_fix.write_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg), review_state)
            repair_states[cfg.tasks_name] = review_state

        state = replace(state, repair_phase="RECHECK")
        write_selection_workflow_state(configs[0].assent_dir, state)
        for cfg in work_configs:
            if not any(task.status == "DONE"
                       for task in Plan.parse(cfg.tasks_dir).tasks):
                continue
            if _verify_focused_locked(cfg) != 0:
                return 1, state
            gitops.ensure_clean(cfg.root, cfg.git_excludes)

        target_ref, target_commit, source_commits = _selection_snapshot(configs)
        if target_ref != state.target_ref or target_commit != state.target_commit:
            raise AssentError(
                "selection target changed during the focused repair sweep")
        state = replace(
            state, source_commits=source_commits,
            step_index=next_action_index, action_status="STALE",
            repair_phase="RECHECK")
        write_selection_workflow_state(configs[0].assent_dir, state)
    return 0, state


def run_selection_workflow(config_path: str, assent_dir, folders,
                           *, sleep: Callable[[float], None] | None = None,
                           now: Callable[[], datetime] | None = None) -> int:
    """Walk and recover the exact selection verification/repair workflow."""
    sleep = sleep or interruptible_sleep
    now = now or (lambda: datetime.now(timezone.utc))
    try:
        assent_dir = os.fspath(assent_dir)
        graph = parse_folder_dependency_graph(assent_dir)
        ordered = tuple(order_folders_by_dependency(graph, set(folders)))
        if len(ordered) != len(folders):
            raise AssentError("exact folder selection is invalid")
        configs = tuple(load_config(config_path, folder) for folder in ordered)
        target_ref, target_commit, source_commits = _selection_snapshot(configs)
        state = read_selection_workflow_state(configs[0].assent_dir)
    except AssentError as error:
        print(f"Selection full_verify: failed ({error})")
        return 1

    steps = configs[0].workflow_integration
    if not any(isinstance(step, WorkflowActionStep) for step in steps):
        steps = steps + (WorkflowActionStep("full_verify"),)
    identity = (ordered, target_ref, target_commit, source_commits)
    pending_repair = bool(
        state is not None and state.folders == ordered
        and state.repair_phase != "NONE")
    if (pending_repair
            and (state.target_ref != target_ref
                 or state.target_commit != target_commit)):
        print("Selection repair stopped: the integration target changed while "
              "durable verifier findings were pending.")
        return 1
    active_repair = pending_repair
    if (state is None or ((state.folders, state.target_ref, state.target_commit,
                           state.source_commits) != identity
                          and not active_repair)):
        state = SelectionWorkflowState(
            ordered, target_ref, target_commit, source_commits, 0)
        write_selection_workflow_state(configs[0].assent_dir, state)
    elif state.step_index >= len(steps):
        if state.action_status == "PASSED":
            # A workflow cursor is not verification evidence.  Re-enter the
            # objective action on every new invocation so the receipt layer can
            # cheaply reuse fresh evidence or reject verifier, stack, shared
            # input, source, or target drift.  This does not rerun a fresh full
            # verifier; verify_*_action owns that receipt decision.
            state = SelectionWorkflowState(
                ordered, target_ref, target_commit, source_commits, 0)
            write_selection_workflow_state(configs[0].assent_dir, state)
        else:
            return _integration_unresolved(
                "the configured integration steps were exhausted while "
                "full_verify was still failing")

    while state.step_index < len(steps):
        step = steps[state.step_index]
        if isinstance(step, WorkflowPlanStep):
            if state.action_status == "PASSED":
                state = replace(state, step_index=state.step_index + 1)
                write_selection_workflow_state(configs[0].assent_dir, state)
                continue
            print("Integration full_verify: repairing durable verifier "
                  f"evidence with configured role {step.role!r}")
            try:
                repair_runner = (_run_selection_merged_repairs
                                 if step.writes and step.produces_verdict
                                 else _run_selection_repairs)
                code, state = repair_runner(
                    configs, state, step, sleep=sleep, now=now)
            except KeyboardInterrupt:
                print("Integration repair interrupted; durable evidence and "
                      "edits were preserved for resume.")
                return 130
            except (AssentError, OSError) as error:
                print(f"Integration repair stopped: {error}")
                return 1
            if code == 0:
                continue
            if code == _INTEGRATION_UNRESOLVED:
                return 0
            return code

        assert step.action == "full_verify"
        print(f"Integration workflow step {state.step_index + 1}/{len(steps)}: "
              "full_verify")
        if len(configs) == 1:
            result = verify_folder_action(
                configs[0], recheck=state.repair_phase == "RECHECK")
            if result.outcome == "TARGET_CONFLICT" and result.source_commits:
                paths = tuple(dict.fromkeys(
                    item.split(":", 1)[1]
                    for item in result.evidence
                    if item.startswith(configs[0].tasks_name + ":")
                    and ":" in item))
                if paths:
                    conflict = SelectionCandidateConflict(
                        configs[0].tasks_name, paths,
                        result.source_commits[0], result.target_commit, (),
                        result.candidate_tree, (), "target_alone")
                    result = replace(
                        result,
                        evidence=result.evidence
                        + (selection_conflict_line(conflict),))
        else:
            result = verify_selected_batch_action(
                config_path, configs[0].assent_dir, ordered,
                recheck=state.repair_phase == "RECHECK")
        if (not result.target_commit or not result.source_commits
                or not result.candidate_tree
                or not result.verification_script_sha256
                or not result.shared_inputs_sha256):
            detail = result.evidence[0] if result.evidence else result.outcome
            print(f"Selection full_verify: {result.outcome} ({detail})")
            return 1
        try:
            with _selection_locks(configs):
                current = _selection_snapshot(configs)
                if current != (state.target_ref, state.target_commit,
                               state.source_commits):
                    raise AssentError(
                        "selection source or target changed after verification")
                if (result.folders != ordered
                        or result.target_commit != state.target_commit
                        or result.source_commits != state.source_commits
                        or result.verification_script_sha256
                        != verification.verifier_digest(configs[0])):
                    raise AssentError(
                        "verification evidence does not match the pending "
                        "selection action")
                state = replace(
                    state, step_index=state.step_index + 1,
                    action="full_verify",
                    action_status="PASSED" if result.passed else "FAILED",
                    action_candidate_tree=result.candidate_tree,
                    action_exit_code=result.exit_code,
                    action_evidence=(
                        result.outcome,
                        f"target: {result.target_commit}",
                        "sources: " + ", ".join(result.source_commits),
                        f"candidate: {result.candidate_tree}",
                    ) + result.evidence,
                    verification_script_sha256=(
                        result.verification_script_sha256),
                    shared_inputs_sha256=result.shared_inputs_sha256,
                    repair_phase="NONE")
                write_selection_workflow_state(configs[0].assent_dir, state)
                if result.passed:
                    _mark_selection_reviews_passed(
                        _selection_worktree_configs(configs),
                        result.candidate_tree, state.step_index)
        except (AssentError, lockfile.LockBusy) as error:
            print(f"Selection full_verify: failed ({error})")
            return 1
        if result.passed:
            print("Integration full_verify: PASS; no reviewer session started.")
            return 0
        if not result.passed and state.step_index >= len(steps):
            return _integration_unresolved(
                "the configured integration steps were exhausted while "
                "full_verify was still failing")
    return (0 if state.action_status == "PASSED" else
            _integration_unresolved(
                "the configured integration steps ended without a passing "
                "full_verify"))


def run_dynamic_selection_workflow(config_path: str, assent_dir) -> int:
    """Snapshot a whole-project run's current verification selection once."""
    try:
        assent_dir = os.fspath(assent_dir)
        root = os.path.dirname(assent_dir)
        main = gitops.main_worktree(root)
        target = gitops.commit_of(
            main, gitops.require_current_branch(main))
        selection, _configs = verification.select_batch_folders(
            config_path, assent_dir, main, target)
    except AssentError as error:
        print(f"Selection full_verify: failed ({error})")
        return 1
    for folder, reason in selection.skipped:
        print(f"Selection full_verify: skip {folder} ({reason})")
    if not selection.folders:
        print("Selection full_verify: no folder has anything left to verify")
        return 0
    return run_selection_workflow(config_path, assent_dir, selection.folders)


def run(cfg: Config, once: bool = False, task_id: str | None = None, *,
        adapter: Adapter | None = None,
        auto_fix_adapter: Adapter | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None) -> int:
    """Run tasks until all are DONE/BLOCKED/SKIP (or only one with once/task_id). Returns the
    process exit code.

    First check folder prerequisites, then take the task folder's file lock; the lock covers
    the whole run (including the long sleeps of quota waiting); if another run is already
    running in the same folder, print a message and fail with exit code 1 without touching
    anything in the working tree. status / check / report are read-only, take no lock, and can
    be used while a run is in progress.
    """
    # The final global-contracts gate.  The CLI refuses earlier with the same
    # message, but a library caller reaches the adapters only through here, so
    # this check -- not that one -- is what guarantees no session can start
    # against a missing or out-of-date ~/.assent contract.
    try:
        contracts.require_contracts()
    except AssentError as e:
        print(f"Global contracts: FAIL ({e})")
        return 1

    try:
        unfinished = find_unfinished_prerequisites(cfg.tasks_dir)
    except AssentError as e:
        print(f"Prerequisite folder gate: FAIL ({e})")
        return 1
    if unfinished:
        print("Prerequisite folders not finished, refusing to run:")
        for prerequisite in unfinished:
            print(f"  - {prerequisite.message()}")
        return 1

    if not has_git_marker(cfg.root):
        print(GIT_REQUIRED_MESSAGE)
        return 1

    if sleep is None:
        # Not time.sleep: a stop request must end a quota segment at once rather
        # than after up to _COUNTDOWN_SEGMENT seconds.
        sleep = interruptible_sleep
    if now is None:
        now = lambda: datetime.now(timezone.utc)  # noqa: E731

    result: int
    try:
        with lockfile.hold_lock(cfg.tasks_dir, cfg.tasks_name):
            result = _run_locked(
                cfg, once, task_id, adapter, auto_fix_adapter, sleep, now)
    except lockfile.LockBusy as e:
        print(str(e))
        return 1
    if result != 0:
        return result

    # Full candidate verification is outside the AI session and outside the
    # folder lock above.  The verification layer reacquires locks in the one
    # safe order used by accept: repository integration, then folder.
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError as e:
        print(f"Failed to parse task folder after run: {e}")
        return 1
    if all(task.status in ("DONE", "SKIP") for task in plan.tasks):
        print(f"integration {cfg.tasks_name}: folder execution complete; "
              "the invocation integration workflow follows")
        try_write_report(cfg)
    return result


def _run_locked(cfg: Config, once: bool, task_id: str | None,
                adapter: Adapter | None,
                auto_fix_adapter: Adapter | None,
                sleep: Callable[[float], None],
                now: Callable[[], datetime]) -> int:
    """The actual run body, after the task folder lock is held."""
    try:
        # Validate the requested folder itself before stack discovery.  This
        # preserves the task-file error as the primary zero-token diagnostic.
        plan = Plan.parse(cfg.tasks_dir)
        trusted_plan = plan
        trusted_contracts = _task_contract_snapshots(plan)
    except AssentError as e:
        print(f"Failed to parse task folder: {e}")
        return 1

    # Every adapter is resolved and its planned invocations proven before the worktree exists,
    # so rotating later can never discover a configuration the vendor would refuse after a
    # session, status write, or Git change.  The injected adapter is the first slot's test seam;
    # production runs resolve every slot through get_adapter().
    adapter_names = list(cfg.adapter_names)
    for workflow in (cfg.workflow_task or (), cfg.workflow_plan,
                     cfg.workflow_integration):
        for step in workflow:
            names = (step.adapters if isinstance(
                step, (WorkflowTaskStep, WorkflowPlanStep)) else None)
            for name in names or ():
                if name not in adapter_names:
                    adapter_names.append(name)
    adapters: dict[str, Adapter] = {}
    try:
        for name in adapter_names:
            adapters[name] = (
                adapter if name == cfg.adapter_names[0] and adapter is not None
                else get_adapter(name, cfg))
    except AssentError as e:
        print(str(e))
        return 1

    rotation = _AdapterRotation(
        cfg.adapter_names, tuple(adapters[name] for name in cfg.adapter_names),
        pool=adapters)
    preflight_failures: list[tuple[str, list[str]]] = []
    whole_plan_workflow = (
        cfg.workflow_task == ()
        and all(task.workflow is None for task in plan.tasks))
    task_adapter_names = list(cfg.adapter_names)
    for step in cfg.workflow_task or ():
        if isinstance(step, WorkflowTaskStep):
            for name in step.adapters or ():
                if name not in task_adapter_names:
                    task_adapter_names.append(name)
    plan_adapter_names = list(dict.fromkeys(
        name for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep)
        for name in step.adapters))
    preflight_names = (plan_adapter_names if whole_plan_workflow
                       else task_adapter_names)
    for name in preflight_names:
        current_adapter = adapters[name]
        if whole_plan_workflow:
            errors = _plan_workflow_capability_errors(
                cfg, current_adapter, plan, name)
        else:
            errors = _workflow_task_capability_errors(
                cfg, current_adapter, plan, name, task_id)
        if errors:
            preflight_failures.append((name, errors))
    if preflight_failures:
        for name, errors in preflight_failures:
            print(f"{name} capability preflight: FAIL "
                  "(refusing before any AI session)")
            for message in errors:
                print(f"  - {message}")
        return 1

    review_rounds = cfg.workflow_plan
    if any(isinstance(step, WorkflowPlanStep) and step.produces_verdict
           for step in review_rounds):
        first_review = next(
            step for step in review_rounds
            if isinstance(step, WorkflowPlanStep) and step.produces_verdict)
        try:
            reviewer_adapter = (
                auto_fix_adapter or get_adapter(first_review.adapter, cfg))
            _session, review_errors = auto_fix_review_capability_errors(
                cfg, reviewer_adapter)
        except AssentError as e:
            review_errors = [str(e)]
        if review_errors:
            print("Auto-fix workflow capability preflight: FAIL "
                  "(refusing before any AI session)")
            for message in review_errors:
                print(f"  - {message}")
            return 1

    if cfg.source_root is None:
        try:
            cfg = _prepare_worktree(cfg)
        except KeyboardInterrupt:
            print("\nInterrupted during worktree setup; no AI session was started.")
            return 130
        except (AssentError, OSError) as e:
            print(f"git worktree setup failed: {e}")
            return 1

    try:
        _recover_or_ensure_clean(cfg, now)
    except AssentError as e:
        print(f"git setup failed: {e}")
        return 1

    active = _ActiveTask()
    blocker_evidence: list[_AutoFixBlockerEvidence] = []
    gate_passes = _FocusedGateLedger()
    try:
        if whole_plan_workflow:
            if task_id is not None:
                print("[workflow].task = [] makes the whole plan one unit; "
                      "--task cannot select part of it")
                return 1
            return _process_plan_workflow(
                cfg, plan, rotation, sleep, now, trusted_contracts)
        # Read the durable state even for an ordinary run.  A pending FAIL is
        # not an additional task status and ordinary execution may still make
        # limited progress, but a complete folder must not silently close over
        # unresolved review evidence just because the invocation omitted the
        # repair authorization.
        existing_auto_fix = _auto_fix_existing_state(cfg)
        # Selection-verification repair is coordinated only after every folder
        # run lock has exited.  A restarted invocation therefore leaves its
        # reopened TODO/WIP tasks untouched here and lets the exact selection
        # cursor resume the durable repair wave in ``_close_run``.
        if (existing_auto_fix is not None
                and existing_auto_fix.review_context
                == "selection_verification"
                and existing_auto_fix.verdict != "PASS"):
            print("Selection verification repair is pending; deferring this "
                  "folder to the exact selection coordinator.")
            return 0
        # A settled SELF-FIXED, UNREVIEWED folder is terminal, not a resumable
        # phase: a later run must not reopen, re-review, or re-run anything for
        # it just because that durable record exists.  A human who reopens a
        # task with rework leaves the folder incomplete, and ordinary execution
        # continues below as usual.
        settled_self_fixed = bool(
            existing_auto_fix is not None
            and existing_auto_fix.self_fixed_unreviewed is not None
            and all(task.status in ("DONE", "SKIP") for task in plan.tasks))
        # A settled REVIEW UNRESOLVED folder ends the loop the same way, but it
        # may legitimately hold a BLOCKED task, so "no task is runnable" is what
        # distinguishes it from a folder a human reopened with rework.
        settled_unresolved = bool(
            existing_auto_fix is not None
            and existing_auto_fix.unresolved_review is not None)
        quiescent_unresolved = settled_unresolved and not any(
            task.status in ("TODO", "WIP") for task in plan.tasks)
        if settled_self_fixed or quiescent_unresolved:
            assert existing_auto_fix is not None
            if settled_self_fixed:
                outcome = existing_auto_fix.self_fixed_unreviewed
                assert outcome is not None
                _print_self_fixed_unreviewed(outcome)
            else:
                unresolved = existing_auto_fix.unresolved_review
                assert unresolved is not None
                _print_unresolved_review(unresolved)
            _print_summary(plan)
            try_write_report(cfg)
            return 0
        resuming_auto_fix = bool(
            existing_auto_fix is not None
            and existing_auto_fix.verdict != "PASS"
            and not settled_self_fixed
            and not settled_unresolved)
        if resuming_auto_fix:
            assert existing_auto_fix is not None
            try:
                reconciled, recovery_note = (
                    _reconcile_auto_fix_recovery_config(
                        cfg, existing_auto_fix))
                if reconciled != existing_auto_fix:
                    auto_fix.write_auto_fix_state(
                        auto_fix.auto_fix_state_path(cfg), reconciled)
                    existing_auto_fix = reconciled
                if recovery_note is not None:
                    print(f"Auto-fix recovery: {recovery_note}.")
            except (AssentError, OSError) as e:
                print(f"Auto-fix recovery refused: {e}.")
                return 1
        review_enabled = any(
            isinstance(step, WorkflowPlanStep) and step.produces_verdict
            for step in review_rounds)
        while not resuming_auto_fix:
            current_plan = Plan.parse(cfg.tasks_dir)
            plan = (_authoritative_status_plan(trusted_plan, current_plan)
                    if review_enabled else current_plan)
            if task_id is not None:
                task = plan.get(task_id)
                if task is None:
                    print(f"Task {task_id} not found in task folder")
                    return 1
                status_by_id = {t.id: t.status for t in plan.tasks}
                unmet = [d for d in task.deps
                         if status_by_id.get(d) not in ("DONE", "SKIP")]
                if unmet:
                    print(f"Task {task_id} has unfinished prerequisites: {', '.join(unmet)}")
                    return 1
                if task.status not in ("TODO", "WIP"):
                    print(f"Task {task_id} is currently {task.status}, "
                          f"not TODO/WIP; skipping")
                    return 0
                resumed = task.status == "WIP"
            else:
                selected = plan.next_task()
                if selected is None:
                    break
                task, resumed = selected

            session = _SessionState()
            workflow = _effective_task_workflow(cfg, task)
            if workflow == ():
                task_plan = Plan([task], plan.dir)
                result = _process_plan_workflow(
                    cfg, task_plan, rotation, sleep, now,
                    {task.id: trusted_contracts[task.id]})
                if result != 0:
                    return result
            else:
                active.task = task
                active.session = session
                _process_task(
                    cfg, task, rotation, sleep, now, session, resumed,
                    blocker_evidence=(blocker_evidence if review_enabled else None),
                    gate_passes=gate_passes if review_enabled else None)
                active.task = None
                active.session = None

            if once or task_id is not None:
                break

        if resuming_auto_fix:
            assert existing_auto_fix is not None
            return _run_auto_fix_repairs(
                cfg, existing_auto_fix, rotation, active,
                injected_reviewer=auto_fix_adapter, sleep=sleep, now=now,
                trusted_plan=trusted_plan,
                trusted_contracts=trusted_contracts)

        if review_enabled:
            review_outcome = _run_auto_fix_review_once(
                cfg, once=once, task_id=task_id,
                injected_adapter=auto_fix_adapter, sleep=sleep, now=now,
                blockers=tuple(blocker_evidence),
                trusted_plan=trusted_plan,
                trusted_contracts=trusted_contracts,
                gate_passes=gate_passes)
            if review_outcome.code != 0:
                if review_outcome.rounds_exhausted:
                    assert review_outcome.state is not None
                    return _auto_fix_finish_rounds_exhausted(
                        cfg, review_outcome.state, now,
                        gate_passes=gate_passes)
                if (review_outcome.state is not None
                        and review_outcome.human_reason is None
                        and not (once or task_id is not None)):
                    return _run_auto_fix_repairs(
                        cfg, review_outcome.state, rotation, active,
                        injected_reviewer=auto_fix_adapter,
                        sleep=sleep, now=now,
                        trusted_plan=trusted_plan,
                        trusted_contracts=trusted_contracts)
                return review_outcome.code
    except KeyboardInterrupt:
        # Ctrl+C on the Windows console reaches the child process (the AI session) too, so
        # the session is terminated by the OS signal; here the engine gathers the produced
        # progress into a wip checkpoint (never discard it) and exits with 130.
        print("\nInterrupt received (Ctrl+C): session terminated, keeping current progress...")
        terminal_checkpoint = bool(
            active.session is not None and active.session.terminal_checkpoint)
        if (not terminal_checkpoint and active.session is not None):
            terminal_checkpoint = _terminal_checkpoint_matches_attempt(
                cfg, active.session.terminal_checkpoint_attempt)
            if terminal_checkpoint:
                active.session.terminal_checkpoint = True
        if (active.task is not None and active.session is not None
                and active.session.identity is not None and not terminal_checkpoint):
            _mark_interrupted_task(
                active.task, active.session.identity,
                "User interrupt; progress kept for next resume", now,
                detail="run received Ctrl+C")
        if terminal_checkpoint:
            print("Terminal auto checkpoint already exists; task remains DONE.")
        else:
            try:
                subject = (_checkpoint_subject(
                    cfg, "wip", active.task, "user interrupt, progress kept")
                    if active.task is not None
                    else f"wip({cfg.tasks_name}): user interrupt, progress kept")
                if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                    print("Progress gathered into a wip checkpoint (git revert it yourself if unsatisfied).")
            except AssentError as e:
                print(f"wip checkpoint failed: {e} (working tree left as is, nothing discarded)")
        try:
            try_write_report(cfg)
        except KeyboardInterrupt:
            # A second stop can arrive while the interrupt handler makes its best-effort
            # report refresh.  The terminal checkpoint, when present, remains authoritative.
            if not terminal_checkpoint:
                raise
        print("Interrupted.")
        return 130
    except _AuthenticationRequired as e:
        print(f"Run stopped: {e}")
        try_write_report(cfg)
        return 1
    except _BillingAbort as e:
        # Distinct from an acceptance failure and from the infrastructure abort below: the
        # account's prepaid balance is exhausted, which no retry or next task can resolve.
        # Keep the current task's progress, leave it unresolved, and stop the whole run.
        print(f"Run aborted (billing/balance): {e}")
        print("The account's prepaid balance is exhausted; retrying cannot fix this. "
              "Top up the account, then rerun to resume from the kept progress.")
        if (active.task is not None and active.session is not None
                and active.session.identity is not None):
            _mark_billing_task(active.task, active.session.identity, str(e), now)
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", active.task, "billing abort, progress kept")
                if active.task is not None
                else f"wip({cfg.tasks_name}): billing abort, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint.")
        except AssentError:
            pass
        try_write_report(cfg)
        return 1
    except (AssentError, OSError) as e:
        print(f"Run aborted (infrastructure error): {e}")
        if (active.task is not None and active.session is not None
                and active.session.identity is not None):
            _mark_interrupted_task(
                active.task, active.session.identity,
                "Aborted on infrastructure error; progress kept for next resume", now,
                detail=str(e))
        try:
            subject = (_checkpoint_subject(
                cfg, "wip", active.task, "infrastructure error abort, progress kept")
                if active.task is not None
                else f"wip({cfg.tasks_name}): infrastructure error abort, progress kept")
            if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
                print("Progress gathered into a wip checkpoint.")
        except AssentError:
            pass
        try_write_report(cfg)
        return 1

    final_plan = Plan.parse(cfg.tasks_dir)
    if not review_rounds and any(
            task.status not in ("DONE", "SKIP") for task in final_plan.tasks):
        _print_summary(final_plan)
        try_write_report(cfg)
        return 0
    pending = _auto_fix_existing_state(cfg)
    # Every non-PASS verdict is an unconfirmed pending state, matching the
    # resume guard above: a durable FIXED is a self-repair no round has yet
    # confirmed, so it must refuse closeout exactly as a FAIL does.  The one
    # settled exceptions are the two terminal outcomes -- SELF-FIXED, UNREVIEWED
    # and REVIEW UNRESOLVED -- whose remaining decision is the human accept
    # rather than another round.
    if (pending is not None and pending.verdict != "PASS"
            and pending.review_context != "selection_verification"
            and pending.self_fixed_unreviewed is None
            and pending.unresolved_review is None):
        print("Auto-fix closeout refused: the folder has a pending "
              f"{pending.verdict} state; "
              "rerun with its current plan workflow policy.")
        _print_summary(final_plan)
        try_write_report(cfg)
        return 1
    _print_summary(final_plan)
    try_write_report(cfg)
    return 0


def _task_contract_snapshots(plan: Plan) -> dict[str, str]:
    """Capture the scheduler-trusted task contracts before a worker can edit them."""
    snapshots: dict[str, str] = {}
    for task in plan.tasks:
        try:
            snapshots[task.id] = task.path.read_text(encoding="utf-8")
        except OSError as e:
            raise AssentError(
                f"Unable to read trusted task contract {task.path}: {e}") from e
    return snapshots


def _contract_text_with_status(text: str, status: str) -> str:
    pattern = re.compile(
        r'^(\s*status\s*=\s*")(TODO|WIP|DONE|BLOCKED|SKIP)("\s*(?:#.*)?)$',
        re.MULTILINE)
    replaced, count = pattern.subn(
        lambda match: f"{match.group(1)}{status}{match.group(3)}", text,
        count=1)
    if count != 1:
        raise AssentError("Trusted task contract has no unique writable status line")
    return replaced


def _authoritative_status_plan(plan: Plan, current: Plan | None = None) -> Plan:
    """Refresh statuses while retaining every checkpoint-trusted structural field."""
    if current is None:
        current = Plan.parse(plan.dir)
    tasks: list[Task] = []
    for task in plan.tasks:
        fresh = current.get(task.id)
        if fresh is None:
            raise AssentError(
                f"Trusted task disappeared during execution: {task.id}")
        tasks.append(replace(task, status=fresh.status))
    return Plan(tasks, plan.dir)


def _authoritative_contracts(
        plan: Plan, trusted_contracts: dict[str, str]) -> dict[str, str]:
    return {
        task.id: _contract_text_with_status(trusted_contracts[task.id], task.status)
        for task in plan.tasks
    }


def _contracts_digest(plan: Plan, contracts_by_id: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for task in sorted(plan.tasks, key=lambda item: item.path.name):
        data = contracts_by_id[task.id].encode("utf-8")
        digest.update(task.path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _auto_fix_management_evidence(
        plan: Plan, contracts_by_id: dict[str, str]) -> str:
    """Reproduce authoritative contracts, untrusted tampering, and journals."""
    sections: list[str] = []
    for task in plan.tasks:
        trusted = contracts_by_id[task.id]
        sections.append(
            f"--- {task.id} task contract (trusted checkpoint; AUTHORITATIVE): "
            f"{task.path} ---\n{trusted.rstrip()}")
        try:
            on_disk = task.path.read_text(encoding="utf-8")
        except OSError as e:
            raise AssentError(
                f"Unable to read auto-fix review task contract {task.path}: {e}") from e
        if on_disk != trusted:
            sections.append(
                f"--- {task.id} current on-disk contract (UNTRUSTED EVIDENCE; "
                "the checkpoint contract remains authoritative) ---\n"
                f"{on_disk.rstrip()}")
        if not task.journal_path.is_file():
            journal = "(journal does not exist)"
        else:
            try:
                journal = task.journal_path.read_text(encoding="utf-8")
            except OSError as e:
                raise AssentError(
                    f"Unable to read auto-fix review journal "
                    f"{task.journal_path}: {e}") from e
        sections.append(
            f"--- {task.id} journal: {task.journal_path} ---\n"
            f"{journal.rstrip()}")
    return "\n\n".join(sections)


def _auto_fix_diff(cfg: Config, old_ref: str, new_ref: str = "HEAD") -> str:
    result = subprocess.run(
        ["git", "diff", "--find-renames", f"{old_ref}..{new_ref}", "--"],
        cwd=str(cfg.root), capture_output=True, encoding="utf-8",
        errors="replace")
    if result.returncode != 0:
        return ("(unable to render diff: "
                f"{_bounded_adapter_diagnostic(result.stderr or result.stdout)})")
    text = result.stdout or "(no cumulative checkpoint diff)"
    if len(text) > _AUTO_FIX_EVIDENCE_LIMIT:
        text = text[:_AUTO_FIX_EVIDENCE_LIMIT] + "\n... [diff truncated]"
    return text.rstrip()


def _auto_fix_changed_paths(cfg: Config, old_ref: str) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{old_ref}..HEAD", "--"],
        cwd=str(cfg.root), capture_output=True, encoding="utf-8",
        errors="replace")
    if result.returncode != 0:
        raise AssentError(
            "Unable to validate auto-fix repair delta paths: "
            + _bounded_adapter_diagnostic(result.stderr or result.stdout))
    return tuple(line.strip().replace("\\", "/")
                 for line in result.stdout.splitlines() if line.strip())


def _auto_fix_dependency_state(plan: Plan) -> str:
    status_by_id = {task.id: task.status for task in plan.tasks}
    lines = []
    for task in plan.tasks:
        unmet = [dep for dep in task.deps
                 if status_by_id.get(dep) not in ("DONE", "SKIP")]
        suffix = f"; unmet deps: {', '.join(unmet)}" if unmet else ""
        lines.append(f"- {task.id}: {task.status}; deps: "
                     f"{', '.join(task.deps) or 'none'}{suffix}")
    return "\n".join(lines)


def _auto_fix_blocker_text(
        blockers: tuple[_AutoFixBlockerEvidence, ...]) -> str:
    if not blockers:
        return "(none; this is a completed-plan review)"
    rendered: list[str] = []
    for item in blockers:
        lines = [
            f"- {item.task.id} [{item.trigger}]: {item.reason}",
        ]
        if item.worker_summary is not None:
            lines.append(
                "  worker journal summary (verbatim): " + item.worker_summary)
        lines.append(f"  focused evidence: {item.focused_evidence}")
        rendered.append("\n".join(lines))
    return "\n".join(rendered)


def _auto_fix_prior_evidence(
        cfg: Config, state: auto_fix.AutoFixState | None) -> str:
    if state is None:
        return "(none; INITIAL review)"
    current = set(state.current_finding_fingerprints)
    lines = ["Prior current findings and recommendations:"]
    for finding in state.findings:
        if finding.fingerprint not in current:
            continue
        lines.append(
            f"- {finding.fingerprint} {finding.task_id or 'unassigned'} "
            f"{finding.path}: {finding.summary}\n"
            f"  evidence: {finding.evidence}\n"
            f"  recommendation: {finding.recommendation}")
    lines.append("Worker dispositions:")
    lines.extend(
        f"- {item.task_id} {item.fingerprint}: "
        f"{item.disposition}; {item.detail}"
        for item in state.worker_dispositions)
    if not state.worker_dispositions:
        lines.append("- none recorded")
    lines.append("Approved scope additions:")
    lines.extend(
        f"- {item.task_id}: {item.path} ({item.path_state})"
        for item in state.approved_scope_additions)
    if not state.approved_scope_additions:
        lines.append("- none")
    # The full brief text (findings, evidence, and a task-scoped diff) is
    # preserved verbatim in the durable state for the worker that receives it,
    # but every part of it duplicates what this prior-evidence text already
    # shows the reviewer above (findings) and below (the diffs) -- repeating
    # it per task made recheck prompts grow with the cascade width for no new
    # signal. Only the task -> fingerprint routing is genuinely new here.
    lines.append("Durable repair briefs (task -> addressed fingerprints; "
                  "full brief text omitted, it repeats the findings and "
                  "diffs already shown above/below):")
    lines.extend(
        f"- {item.task_id}: {', '.join(item.finding_fingerprints)}"
        for item in state.repair_briefs)
    if not state.repair_briefs:
        lines.append("- none")
    lines.append("Repair-only relevant diff:")
    lines.append(_auto_fix_diff(cfg, state.source_tree))
    lines.append("Prior observed states:")
    lines.extend(
        f"- {item.source_tree}: {', '.join(item.finding_fingerprints) or 'PASS'}"
        for item in state.observed_states)
    return "\n".join(lines)


def _auto_fix_round_policy(round_index: int, total: int) -> str:
    """State this round's position, how many follow, and the final-round rule."""
    text = _AUTO_FIX_ROUND_POLICY.format(
        position=round_index + 1, total=total,
        remaining=max(total - round_index - 1, 0))
    if round_index + 1 >= total:
        text += "\n\n" + _AUTO_FIX_FINAL_ROUND_POLICY
    return text


def _auto_fix_review_identity(
        cfg: Config, plan: Plan, focused_evidence: str, *,
        focused_identity: str | None = None,
        contracts_by_id: dict[str, str] | None = None,
        review_context: str = "completed_folder",
        review_stage: str = "initial",
        round_index: int = 0,
        blockers: tuple[_AutoFixBlockerEvidence, ...] = (),
        previous: auto_fix.AutoFixState | None = None,
        shared_contract: shared_paths.Contract | None = None,
        ) -> tuple[str, str, str, str]:
    """Return source tree, task-plan digest, prompt text, and prompt digest."""
    source_tree = gitops.tree_of(cfg.root, "HEAD")
    resolved_base = resolve_stack_state(cfg).base.resolved_base
    base_ref = gitops.merge_base(cfg.root, resolved_base, "HEAD")
    if contracts_by_id is None:
        contracts_by_id = {
            task.id: task.path.read_text(encoding="utf-8")
            for task in plan.tasks
        }
    rounds = tuple(
        step for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep))
    can_write = (
        review_context != "blocked_adjudication"
        and round_index < len(rounds)
        and rounds[round_index].writes)
    reviewed_material = dict(
        agents_md_path=_agents_md_absolute_path_for_prompt(cfg),
        instructions_path=contracts.instructions_path(),
        folder=cfg.tasks_name,
        base_ref=base_ref,
        source_tree=source_tree,
        review_context=review_context.upper(),
        review_stage=review_stage.upper(),
        workflow_role=(rounds[round_index].role
                       if round_index < len(rounds)
                       else "unavailable"),
        role_policy=("\n".join(
            ability.prompt for ability in
            rounds[round_index].resolved_role.abilities)
            if round_index < len(rounds) else "- none"),
        write_policy=(_AUTO_FIX_MERGED_WRITE_POLICY
                      if can_write else _AUTO_FIX_READ_ONLY_POLICY),
        scope_policy=(_AUTO_FIX_SCOPE_WRITE_POLICY
                      if can_write else _AUTO_FIX_SCOPE_READ_ONLY_POLICY),
        round_policy=(
            _AUTO_FIX_BLOCKED_ROUND_POLICY
            if review_context == "blocked_adjudication"
            else _auto_fix_round_policy(
                round_index, len(rounds) or 1)),
        context_policy=(
            _AUTO_FIX_BLOCKED_POLICY
            if review_context == "blocked_adjudication"
            else _AUTO_FIX_COMPLETED_POLICY),
        stage_policy=(
            _AUTO_FIX_RECHECK_POLICY
            if review_stage == "recheck" else _AUTO_FIX_INITIAL_POLICY),
        cumulative_diff=_auto_fix_diff(cfg, base_ref),
        blocker_evidence=_auto_fix_blocker_text(blockers),
        focused_evidence=focused_evidence,
        dependency_state=_auto_fix_dependency_state(plan),
        management_evidence=_auto_fix_management_evidence(
            plan, contracts_by_id),
    )
    prompt = _AUTO_FIX_REVIEW_PROMPT.format(
        prior_evidence=_auto_fix_prior_evidence(cfg, previous),
        **reviewed_material)
    identity_material = dict(reviewed_material)
    if focused_identity is not None:
        # A reused authoritative PASS is annotated for the reviewer and the
        # terminal log, but must not move the review identity: one command, one
        # source tree and one shared-input state digest the same whether the
        # scheduler ran the command once or twice.
        identity_material["focused_evidence"] = focused_identity
    identity = _AUTO_FIX_REVIEW_PROMPT.format(
        prior_evidence=_AUTO_FIX_PRIOR_EVIDENCE_IDENTITY, **identity_material)
    if shared_contract is None:
        try:
            shared_contract = _shared_paths_contract(cfg)
        except AssentError:
            pass
    if shared_contract is not None:
        clause = shared_paths.review_record_clause(shared_contract)
        prompt += clause
        identity += clause
    plan_digest = _contracts_digest(plan, contracts_by_id)
    prompt_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return source_tree, plan_digest, prompt, prompt_digest


def _auto_fix_existing_state(cfg: Config) -> auto_fix.AutoFixState | None:
    path = auto_fix.auto_fix_state_path(cfg)
    if not path.exists():
        return None
    return auto_fix.read_auto_fix_state(path)


def _reconcile_auto_fix_recovery_config(
        cfg: Config, state: auto_fix.AutoFixState,
        ) -> tuple[auto_fix.AutoFixState, str | None]:
    """Restart stale orchestration under the current workflow without losing evidence."""
    rounds = tuple(
        step for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep))
    if not rounds:
        raise AssentError(
            "the pending review state requires a configured plan review "
            "sequence before repair or closeout can resume")
    stored = (state.reviewer_adapter, state.reviewer_model,
              state.reviewer_effort)
    position = state.reviewer_step_index
    configured = [
        (item.role, item.adapter, item.requested_model,
         effort_identity(item.requested_effort))
        for item in rounds]
    expected = (state.reviewer_role, *stored)
    matches = position < len(configured) and configured[position] == expected
    cursor_valid = state.workflow_step_index <= len(rounds)
    if state.workflow_step_index == 0:
        return state, None
    if matches and cursor_valid:
        return state, None
    # A zero workflow cursor is the durable marker that this state has been
    # rebound. The old reviewer position and identity remain evidence until
    # the first current reviewer replaces them.
    restarted = auto_fix.restart_workflow_cursor(state)
    return restarted, (
        "the configured plan review sequence changed; preserved the pending "
        "findings and source work, then restarted review from its first step")


def _auto_fix_surface_snapshot(cfg: Config) -> auto_fix.ProjectSurfaceSnapshot:
    """Capture only this review's protected project-management surfaces."""
    stable = [
        cfg.assent_dir / "verify.py",
        cfg.assent_dir / "manifest.toml",
        cfg.assent_dir / "_batch_verification.toml",
        cfg.assent_dir / "_archived.toml",
        cfg.assent_dir / "_archive",
    ]
    management_root = cfg.assent_dir.absolute()
    for source in cfg.sources:
        if source.path is None:
            continue
        try:
            source.path.absolute().relative_to(management_root)
        except ValueError:
            continue
        stable.append(source.path)
    return auto_fix.snapshot_project_surface(
        cfg.root, cfg.assent_dir, cfg.source_root,
        tasks_dir=cfg.tasks_dir, stable_management_files=stable,
        prune_ignored_source_directories=True)


def _auto_fix_shared_path_changes(
        before: shared_paths.Contract,
        after: shared_paths.Contract) -> set[str]:
    """Return controlled shared-path changes that a review may settle."""
    if not before.needs_review or not after.settled:
        return set()
    paths = set(before.prior_paths) | set(after.paths)
    return {"management:manifest.toml", *(f"source:{path}" for path in paths)}


def _validated_auto_fix_shared_paths_decision(
        cfg: Config, before: shared_paths.Contract,
        after: shared_paths.Contract | None,
        record: auto_fix.ReviewRecord,
        ) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
    """Validate a requested plan-review decision without applying it."""
    decision = record.shared_paths
    if not before.needs_review:
        if decision is not None:
            raise AssentError(
                "shared_paths must be null when the contract needs no review")
        return None
    if after is not None and after.settled:
        # Accept the legacy same-session CLI operation during migration. If a
        # structured decision is also present, it must describe that exact
        # already-applied profile.
        if decision is None:
            return None
        main = gitops.main_worktree(cfg.root)
        declared, watched = shared_paths.validate_review_decision(
            main, cfg.root, decision.paths, decision.watch)
        profile = after.profile
        if (profile is None or declared != after.paths
                or watched != profile.watch):
            raise AssentError(
                "shared_paths does not match the profile applied in this session")
        return None
    if decision is None:
        raise AssentError(
            "shared_paths must contain the required paths and watch decision")
    main = gitops.main_worktree(cfg.root)
    return shared_paths.validate_review_decision(
        main, cfg.root, decision.paths, decision.watch)


def _auto_fix_surface_change(
        before: auto_fix.ProjectSurfaceSnapshot,
        cfg: Config, before_head: str,
        before_status: gitops.WorkingTreeStatus,
        before_primary_head: str | None,
        before_primary_status: gitops.WorkingTreeStatus | None) -> tuple[str, ...]:
    after = _auto_fix_surface_snapshot(cfg)
    changed = list(before.changed_paths(after))
    if gitops.commit_of(cfg.root, "HEAD") != before_head:
        changed.append("source:.git/HEAD")
    if gitops.working_tree_status(cfg.root, cfg.git_excludes) != before_status:
        changed.append("source:.git/index-or-status")
    if cfg.source_root is not None:
        if gitops.commit_of(cfg.source_root, "HEAD") != before_primary_head:
            changed.append("primary:.git/HEAD")
        if (gitops.working_tree_status(cfg.source_root, cfg.git_excludes)
                != before_primary_status):
            changed.append("primary:.git/index-or-status")
    return tuple(sorted(set(changed)))


def _blocked_evidence_from_journals(plan: Plan) -> tuple[_AutoFixBlockerEvidence, ...]:
    """Recover durable blocker reasons for a later full auto-fix invocation."""
    blockers: list[_AutoFixBlockerEvidence] = []
    for task in plan.tasks:
        if task.status != "BLOCKED":
            continue
        try:
            entries = read_entries(task.journal_path)
        except (AssentError, OSError):
            continue
        reason: str | None = None
        worker_summary: str | None = None
        focused = ("NOT RUN: no captured focused result is present for this "
                   "durable scheduler blocker.")
        for entry in reversed(entries):
            if entry.get("event") not in ("auto_fix_blocker", "blocked"):
                continue
            summary = str(entry.get("summary") or "")
            detail = str(entry.get("detail") or "")
            if "Focused evidence:\n" in detail:
                focused = detail.split("Focused evidence:\n", 1)[1]
            if entry.get("by") != "scheduler":
                reason = "Execution AI self-marked BLOCKED"
                worker_summary = summary or "(empty summary)"
                focused = ("NOT RUN: self-marked BLOCKED closeout legitimately "
                           "skips the focused gate.")
            elif (entry.get("event") == "auto_fix_blocker"
                  and focused.startswith("NOT RUN")
                  and "self-marked BLOCKED" in focused):
                reason = "Execution AI self-marked BLOCKED"
                marker = "Worker journal summary (verbatim):\n"
                if marker in detail:
                    worker_summary = detail.split(marker, 1)[1].split(
                        "\nFocused evidence:\n", 1)[0]
                elif summary != reason:
                    # Version-2 blocker entries used the worker summary as the
                    # scheduler entry summary. Preserve it during recovery.
                    worker_summary = summary or "(empty summary)"
            else:
                reason = summary or None
                if reason and "Verify command exit code is non-zero" in reason:
                    focused = reason
            break
        if reason is None:
            continue
        focused_lower = focused.lower()
        trigger = ("focused_gate_failure"
                   if ("focused" in focused_lower
                       or "verify command" in focused_lower)
                   and not focused.startswith("NOT RUN")
                   else "worker_blocked")
        blockers.append(_AutoFixBlockerEvidence(
            task, trigger, reason, focused, worker_summary))
    return tuple(blockers)


def _merge_blocker_evidence(
        plan: Plan, current: tuple[_AutoFixBlockerEvidence, ...],
        recovered: tuple[_AutoFixBlockerEvidence, ...],
        ) -> tuple[_AutoFixBlockerEvidence, ...]:
    """Return one current-or-durable blocker record per task in plan order."""
    by_id = {item.task.id: item for item in recovered}
    by_id.update((item.task.id, item) for item in current)
    return tuple(by_id[task.id] for task in plan.tasks if task.id in by_id)


def _restore_trusted_contracts_after_adjudication(
        plan: Plan, contracts_by_id: dict[str, str],
        now: Callable[[], datetime]) -> None:
    """Discard no source output while refusing to adopt worker contract tampering."""
    for trusted in plan.tasks:
        current = parse_task_file(trusted.path)
        changed_fields = same_except_status(trusted, current)
        if not changed_fields:
            continue
        authoritative = contracts_by_id[trusted.id]
        try:
            trusted.path.write_text(
                authoritative, encoding="utf-8", newline="")
        except OSError as e:
            raise AssentError(
                f"Unable to restore trusted checkpoint contract {trusted.path}: {e}") from e
        restored = parse_task_file(trusted.path)
        if same_except_status(trusted, restored) or restored.status != trusted.status:
            raise AssentError(
                f"Trusted checkpoint contract restoration failed: {trusted.path}")
        append_entry(
            trusted.journal_path, by="scheduler",
            event="auto_fix_contract_restore",
            summary=("Restored the trusted checkpoint task contract after "
                     "blocked adjudication"),
            detail=("The worker's on-disk structural edits were reviewed only as "
                    "untrusted evidence and were not adopted; changed fields: "
                    + ", ".join(changed_fields)),
            time_str=now().isoformat(timespec="seconds"))


def _run_auto_fix_review_once(
        cfg: Config, *, once: bool, task_id: str | None,
        injected_adapter: Adapter | None,
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
        blockers: tuple[_AutoFixBlockerEvidence, ...] = (),
        trusted_plan: Plan | None = None,
        trusted_contracts: dict[str, str] | None = None,
        gate_passes: _FocusedGateLedger | None = None,
        ) -> _AutoFixReviewOutcome:
    """Run one merged reviewer-fixer round or quiescent blocked adjudication."""
    rounds = tuple(
        step for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep))
    if not rounds:
        return _AutoFixReviewOutcome(0)
    first_action_index = next(
        index for index, step in enumerate(cfg.workflow_plan)
        if isinstance(step, WorkflowActionStep))
    initial_role_count = sum(
        isinstance(step, WorkflowPlanStep)
        for step in cfg.workflow_plan[:first_action_index])
    # The folder walks the configured reviewer list position by position, and
    # the durable index is what survives a restart mid-sequence.
    existing = _auto_fix_existing_state(cfg)
    round_index = existing.workflow_step_index if existing is not None else 0
    while round_index < len(rounds) and not rounds[round_index].produces_verdict:
        round_index += 1

    plan = (_authoritative_status_plan(trusted_plan)
            if trusted_plan is not None else Plan.parse(cfg.tasks_dir))
    if trusted_contracts is None:
        trusted_contracts = _task_contract_snapshots(plan)
    contracts_by_id = _authoritative_contracts(plan, trusted_contracts)
    incomplete = [task for task in plan.tasks
                  if task.status not in ("DONE", "SKIP")]
    blocked = [task for task in incomplete if task.status == "BLOCKED"]
    runnable = plan.next_task()
    limited = once or task_id is not None
    review_context = "completed_folder"
    review_session_started = False
    initial_quality_review = bool(
        review_context == "completed_folder"
        and not incomplete
        and initial_role_count
        and (existing is None or existing.verdict == "PASS")
        and round_index < initial_role_count)
    rebound_review = bool(
        existing is not None
        and existing.verdict != "PASS"
        and existing.workflow_step_index == 0)

    if incomplete and existing is None:
        shown = ", ".join(f"{task.id}={task.status}" for task in incomplete)
        suffix = " after the limited run" if limited else ""
        print(f"Plan workflow review deferred{suffix}; workflow.task owns "
              f"every incomplete task ({shown}).")
        return _AutoFixReviewOutcome(0)

    def finish(outcome: _AutoFixReviewOutcome) -> _AutoFixReviewOutcome:
        """Close scheduler-owned review state after its source work is durable."""
        nonlocal review_session_started
        if review_session_started:
            try:
                clean = gitops.working_tree_status(
                    cfg.root, cfg.git_excludes).is_clean
                if clean:
                    auto_fix.clear_auto_fix_review_session(cfg)
                    review_session_started = False
            except (AssentError, OSError) as e:
                print(f"Auto-fix review session closeout failed: {e}")
                return _AutoFixReviewOutcome(1, outcome.state, str(e))
        if review_context != "blocked_adjudication":
            return outcome
        try:
            _restore_trusted_contracts_after_adjudication(
                plan, contracts_by_id, now)
        except AssentError as e:
            print("Auto-fix adjudication evidence was preserved, but trusted "
                  f"contract restoration failed: {e}")
            return _AutoFixReviewOutcome(1, outcome.state, str(e))
        return outcome

    if incomplete:
        selected_blocker = bool(blockers)
        if (not blocked or runnable is not None
                or (limited and not selected_blocker)):
            suffix = " after the limited run" if limited else ""
            shown = ", ".join(f"{task.id}={task.status}" for task in incomplete)
            print(f"Auto-fix plan review deferred{suffix}; plan is incomplete "
                  f"({shown}).")
            return _AutoFixReviewOutcome(0)
        review_context = "blocked_adjudication"
        blockers = _merge_blocker_evidence(
            plan, blockers, _blocked_evidence_from_journals(plan))
        required = {item.id for item in blocked}
        available = {item.task.id for item in blockers}
        missing = sorted(required - available)
        if not blockers or missing:
            suffix = f" ({', '.join(missing)})" if missing else ""
            print("Auto-fix blocked adjudication refused: durable BLOCKED tasks "
                  f"have no readable scheduler evidence{suffix}.")
            return _AutoFixReviewOutcome(
                1, human_reason="blocked tasks have no durable scheduler evidence")

        if any(item.trigger == "focused_gate_failure" for item in blockers):
            print("Task focused_test repair budget was exhausted; the plan "
                  "workflow is not used to repair a task-layer failure.")
            return finish(_AutoFixReviewOutcome(0))

        read_only_index = next((
            index for index in range(round_index, len(rounds))
            if rounds[index].produces_verdict and not rounds[index].writes), None)
        if read_only_index is None:
            print("Blocked adjudication requires a configured read-only verdict "
                  "role; no writable role was opened with reduced permissions. "
                  "The BLOCKED evidence remains for human decision.")
            return finish(_AutoFixReviewOutcome(0))
        round_index = read_only_index

    rounds_exhausted = round_index >= len(rounds)
    review_index = min(round_index, len(rounds) - 1)
    review = rounds[review_index]
    assert review.adapter is not None
    assert review.model is not None
    assert review.requested_model is not None

    done = [task for task in plan.tasks if task.status == "DONE"]
    if review_context == "completed_folder" and not done:
        print("Auto-fix plan review: all tasks are SKIP; no implementation review session needed.")
        return _AutoFixReviewOutcome(0)

    focused_lines: list[str] = []
    # The identity lines carry the same evidence without the reuse annotation,
    # so reuse never changes the reviewer's freshness or convergence boundary.
    identity_lines: list[str] = []
    seen: set[str] = set()
    current_fingerprints = (
        set(existing.current_finding_fingerprints)
        if existing is not None else set())
    pending_focused_failure = bool(
        existing is not None
        and existing.verdict == "FAIL"
        and existing.review_context == "completed_folder"
        and existing.source_tree == gitops.tree_of(cfg.root, "HEAD")
        and any(
            finding.fingerprint in current_fingerprints
            and finding.summary == "Final focused verification failed"
            for finding in existing.findings))
    if review_context == "completed_folder" and pending_focused_failure:
        print("Plan focused_sweep: reusing durable failure evidence for review.")
        for finding in existing.findings:
            if finding.fingerprint not in current_fingerprints:
                continue
            line = f"- FAIL: {finding.evidence}"
            focused_lines.append(line)
            identity_lines.append(line)
    elif (review_context == "completed_folder"
          and not initial_quality_review and not rebound_review):
        print("Auto-fix plan review: running final distinct focused checks.")
    elif review_context == "completed_folder":
        print("Plan quality review: reviewing the completed cumulative worktree "
              "before focused_sweep.")
        focused_lines.append(
            "- NOT RUN: the current workflow must re-adjudicate durable "
            "findings before focused_sweep"
            if rebound_review else
            "- NOT RUN: focused_sweep follows the initial plan quality review")
        identity_lines.append(focused_lines[-1])
    else:
        print("Auto-fix blocked adjudication: using durable task failure evidence; "
              "no focused command is run by the reviewer gate.")
        focused_lines.extend(
            f"- {item.task.id}: {item.focused_evidence}" for item in blockers)
        identity_lines.extend(focused_lines)
    distinct: list[str] = []
    for task in (done if review_context == "completed_folder"
                 and not initial_quality_review
                 and not rebound_review
                 and not pending_focused_failure else ()):
        if task.verify in seen:
            continue
        seen.add(task.verify)
        distinct.append(task.verify)
    # Reuse is decided first and still wins, so a command the scheduler already
    # proved against this tree never enters the merged union.  Only the
    # commands this sweep would really execute are merged, on one fixed tree.
    reused = {command: gate_passes is not None
              and gate_passes.reusable(cfg, command)
              for command in distinct}
    merged = _merged_unittest_passes(
        cfg, [command for command in distinct if not reused[command]])
    for command in distinct:
        if reused[command]:
            print(f"  verify: {command}")
            print("  reused authoritative PASS (scheduler gate, exit 0)")
            focused_lines.append(
                f"- PASS (reused authoritative PASS): {command}")
            identity_lines.append(f"- PASS: {command}")
            continue
        verify_result = merged.get(command)
        if verify_result is None:
            verify_result = _verify_subprocess(cfg, command)
        _show_verify_result(command, verify_result)
        if verify_result.returncode != 0:
            diagnostic = _bounded_adapter_diagnostic(
                verify_result.stderr or verify_result.stdout or "")
            focused_lines.append(
                f"- FAIL ({verify_result.returncode}): {command}; {diagnostic}")
            identity_lines.append(focused_lines[-1])
            owners = [item for item in done if item.verify == command]
            record = auto_fix.ReviewRecord("FAIL", tuple(
                auto_fix.ReviewFinding(
                    item.id, auto_fix.scheduler_finding_path(item.scope[0]),
                    "Final focused verification failed",
                    f"exit {verify_result.returncode}: {command}; {diagnostic}")
                for item in owners))
            record = auto_fix.validate_review_findings(record, plan)
            source_tree, plan_digest, _prompt, prompt_digest = (
                _auto_fix_review_identity(
                    cfg, plan, "\n".join(focused_lines),
                    focused_identity="\n".join(identity_lines),
                    contracts_by_id=contracts_by_id,
                    round_index=review_index))
            # The scheduler authored this failure; no reviewer round ran, so
            # the folder's round position stays exactly where it was.
            state = auto_fix.state_for_review(
                record, previous=existing,
                source_tree=source_tree,
                task_plan_sha256=plan_digest,
                review_prompt_sha256=prompt_digest,
                reviewer_adapter=review.adapter,
                reviewer_role=review.role,
                reviewer_step_index=review_index,
                reviewer_model=review.requested_model,
                reviewer_effort=effort_identity(review.requested_effort))
            state = _auto_fix_attach_repair_briefs(
                cfg, plan, state,
                blocker_evidence=_auto_fix_blocker_text(blockers),
                focused_evidence="\n".join(focused_lines))
            auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
            print("Auto-fix plan review: focused verification failed; "
                  "scheduler findings were preserved and the reviewer was not started.")
            dirty = not gitops.working_tree_status(
                cfg.root, cfg.git_excludes).is_clean
            reason = ("focused verification changed the source worktree"
                      if dirty else None)
            return _AutoFixReviewOutcome(1, state, reason)
        focused_lines.append(f"- PASS: {command}")
        identity_lines.append(focused_lines[-1])
    if not gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        print("Auto-fix plan review: focused verification changed the source worktree; "
              "reviewer was not started and the exact changes are preserved.")
        return _AutoFixReviewOutcome(
            1, human_reason="focused verification changed the source worktree")

    # A passing scheduler action completes the plan layer.  Review roles are
    # failure handlers, so no AI session is opened merely to confirm a
    # mechanically passing focused_sweep.
    if (review_context == "completed_folder"
            and not pending_focused_failure
            and not initial_quality_review
            and not rebound_review):
        if existing is None or existing.verdict == "PASS":
            print("Plan focused_sweep: PASS; no reviewer session started.")
            return finish(_AutoFixReviewOutcome(0, existing))
        pass_context = existing.review_context
        source_tree, plan_digest, _prompt, prompt_digest = (
            _auto_fix_review_identity(
                cfg, plan, "\n".join(focused_lines),
                focused_identity="\n".join(identity_lines),
                contracts_by_id=contracts_by_id,
                review_context=pass_context, review_stage="recheck",
                round_index=review_index, previous=existing))
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("PASS", ()), previous=existing,
            review_stage="recheck", source_tree=source_tree,
            task_plan_sha256=plan_digest,
            review_prompt_sha256=prompt_digest,
            reviewer_adapter=existing.reviewer_adapter,
            reviewer_role=existing.reviewer_role,
            reviewer_step_index=review_index,
            reviewer_model=existing.reviewer_model,
            reviewer_effort=existing.reviewer_effort,
            review_context=pass_context,
            failure_trigger=existing.failure_trigger,
            workflow_step_index=round_index)
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        print("Plan focused_sweep: PASS after repair; no reviewer session started.")
        return finish(_AutoFixReviewOutcome(0, state))

    if rounds_exhausted:
        print(f"Auto-fix plan review: all {len(rounds)} configured workflow "
              "steps have been used; no further verdict step starts.")
        return finish(_AutoFixReviewOutcome(
            1, existing, human_reason="configured workflow steps are exhausted",
            rounds_exhausted=True))

    if existing is not None and existing.verdict == "PASS":
        # A settled PASS keeps the lineage it was decided under.  Deriving a
        # fresh stage and context here would rebuild a recheck PASS as an
        # initial completed-plan review, fail its own freshness check, and
        # start a reviewer session whose state replaces the cumulative ledger,
        # review transitions and technical-debt agenda.  So reuse compares the
        # durable lineage against the current material only.
        reuse_tree, reuse_plan_digest, _reuse_prompt, reuse_digest = (
            _auto_fix_review_identity(
                cfg, plan, "\n".join(focused_lines),
                focused_identity="\n".join(identity_lines),
                contracts_by_id=contracts_by_id,
                review_context=existing.review_context,
                review_stage=existing.review_stage,
                round_index=round_index,
                blockers=blockers))
        if (existing.reviewer_adapter in review.adapters
                and auto_fix.auto_fix_state_is_fresh(
                existing,
                source_tree=reuse_tree,
                task_plan_sha256=reuse_plan_digest,
                review_prompt_sha256=reuse_digest,
                reviewer_adapter=existing.reviewer_adapter,
                reviewer_role=review.role,
                reviewer_model=existing.reviewer_model,
                reviewer_effort=existing.reviewer_effort,
                review_context=existing.review_context,
                failure_trigger=existing.failure_trigger)):
            print("Auto-fix plan review: reusing exact fresh PASS; no reviewer session started.")
            return finish(_AutoFixReviewOutcome(0, existing))

    review_stage = ("recheck"
                    if existing is not None and existing.verdict != "PASS"
                    else "initial")
    review_previous = existing if review_stage == "recheck" else None
    # The finding ledger, review transitions and technical-debt agenda belong
    # to the folder rather than to one review stage.  A later initial review
    # continues them, so no finding or plan change restarts the finite round
    # sequence; only a recheck may cite prior findings when validating
    # transition identity.
    ledger_previous = existing
    if review_stage == "recheck":
        review_context = existing.review_context
    failure_trigger = None
    if review_context == "blocked_adjudication":
        if blockers:
            failure_trigger = (
                "focused_gate_failure"
                if any(item.trigger == "focused_gate_failure" for item in blockers)
                else "worker_blocked")
        else:
            # Only an inherited recheck context reaches a blocked adjudication
            # with no current collection: the repaired folder is complete, so
            # the awaiting-review call carries no blocker.  The event under
            # adjudication is still the original one, and an empty collection
            # must not reclassify a focused gate failure as a worker block.
            assert existing is not None
            failure_trigger = existing.failure_trigger
    try:
        review_shared_contract = _shared_paths_contract(cfg)
    except AssentError as e:
        print(f"Auto-fix shared-path classification failed: {e}")
        return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))
    source_tree, plan_digest, prompt, prompt_digest = _auto_fix_review_identity(
        cfg, plan, "\n".join(focused_lines),
        focused_identity="\n".join(identity_lines),
        contracts_by_id=contracts_by_id,
        review_context=review_context, review_stage=review_stage,
        round_index=round_index,
        blockers=blockers, previous=review_previous,
        shared_contract=review_shared_contract)

    freshness = dict(
        source_tree=source_tree,
        task_plan_sha256=plan_digest,
        review_prompt_sha256=prompt_digest,
        reviewer_adapter=review.adapter,
        reviewer_role=review.role,
        reviewer_step_index=round_index,
        reviewer_model=review.requested_model,
        reviewer_effort=effort_identity(review.requested_effort),
        review_context=review_context,
        failure_trigger=failure_trigger,
    )
    try:
        review_rotation = _workflow_step_rotation(
            cfg, review, injected_adapter)
        review_sessions = {
            name: _plan_step_session(
                cfg, candidate, plan, review, name)
            for name, candidate in zip(
                review_rotation.names, review_rotation.adapters)
        }
    except AssentError as e:
        print(f"Auto-fix reviewer resolution failed: {e}")
        return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))
    # The top-level run gate preflighted every distinct configured identity
    # before any task or reviewer session started.

    if review.writes:
        try:
            auto_fix.write_auto_fix_review_session(cfg, _plan_scope(plan))
            review_session_started = True
        except AssentError as e:
            print(f"Auto-fix review session boundary failed: {e}")
            return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))

    baseline = _auto_fix_surface_snapshot(cfg)
    baseline_head = gitops.commit_of(cfg.root, "HEAD")
    baseline_status = gitops.working_tree_status(cfg.root, cfg.git_excludes)
    baseline_primary_head = (gitops.commit_of(cfg.source_root, "HEAD")
                             if cfg.source_root is not None else None)
    baseline_primary_status = (
        gitops.working_tree_status(cfg.source_root, cfg.git_excludes)
        if cfg.source_root is not None else None)
    invalid_attempts = 0
    attempt_prompt = prompt
    failed_adapters: set[str] = set()
    while True:
        reviewer = review_rotation.adapter
        session = review_sessions[review_rotation.name]
        print(f"Auto-fix review session: {session.agent} | "
              f"{review.model}->{session.requested_model} | "
              f"{effort_identity(session.effort)}->"
              f"{effort_identity(session.requested_effort)}")
        try:
            result = _invoke_adapter(
                cfg, reviewer, session.agent, attempt_prompt,
                session.requested_model, session.requested_effort, cfg.root,
                context_kind="plan",
                context_id=f"workflow.plan[{round_index}]", structured=True)
        except KeyboardInterrupt:
            changed = _auto_fix_surface_change(
                baseline, cfg, baseline_head, baseline_status,
                baseline_primary_head, baseline_primary_status)
            if changed:
                print("Auto-fix reviewer interruption interval contains project writes; "
                      "exact edits are preserved: "
                      + ", ".join(changed[:8]))
                return finish(_AutoFixReviewOutcome(
                    130, human_reason="reviewer interrupted"))
            else:
                print("Auto-fix reviewer interrupted; no verdict was recorded.")
            return finish(_AutoFixReviewOutcome(
                130, human_reason="reviewer interrupted"))
        except (AssentError, OSError) as e:
            changed = _auto_fix_surface_change(
                baseline, cfg, baseline_head, baseline_status,
                baseline_primary_head, baseline_primary_status)
            suffix = (f"; project writes preserved: {', '.join(changed[:8])}"
                      if changed else "")
            print(f"Auto-fix reviewer infrastructure failure: {e}{suffix}")
            outcome = _AutoFixReviewOutcome(1, human_reason=str(e))
            return finish(outcome)

        changed = _auto_fix_surface_change(
            baseline, cfg, baseline_head, baseline_status,
            baseline_primary_head, baseline_primary_status)
        try:
            shared_contract_after = _shared_paths_contract(cfg)
        except AssentError:
            shared_contract_after = None
        if shared_contract_after is not None:
            allowed = _auto_fix_shared_path_changes(
                review_shared_contract, shared_contract_after)
            changed = tuple(path for path in changed if path not in allowed)
        # A merged reviewer-fixer round may repair source files, so the general
        # before/after comparison still runs for every round but the refusal is
        # decided against the verdict below.  Writes no round may ever make --
        # the management plane, the primary worktree, Git state itself, and
        # anything at all during a read-only blocked adjudication -- still
        # refuse here, before the verdict is even parsed.
        forbidden = _auto_fix_forbidden_writes(changed)
        if changed and (review_context == "blocked_adjudication"
                        or not review.writes or forbidden):
            reported = forbidden or list(changed)
            shown = ", ".join(reported[:8]) + (
                " ..." if len(reported) > 8 else "")
            print("Protected project writes were detected during the reviewer interval; "
                  "the verdict was ignored and the exact edits are preserved "
                  f"for explicit human recovery ({shown}).")
            return finish(_AutoFixReviewOutcome(
                1, human_reason="reviewer project writes detected"))

        if (result.checkpoint_resume and not result.quota_exhausted
                and not result.stalled and result.exit_code != 0):
            print("Auto-fix reviewer requested immediate checkpoint-resume continuation.")
            continue
        if result.quota_exhausted:
            if len(review_rotation.names) == 1:
                print("Auto-fix reviewer quota exhausted; waiting before resuming the same review.")
                _wait_for_quota(cfg, result.reset_at, sleep, now)
            elif review_rotation.advance_after_quota(failed_adapters):
                if review_rotation.auth_failed:
                    print("Every recoverable review adapter is quota-exhausted; "
                          "authentication-failed candidates remain skipped while waiting.")
                else:
                    print("Every declared review adapter is quota-exhausted; "
                          "waiting before continuing.")
                _wait_for_rotation(cfg, sleep)
            else:
                print(f"Auto-fix reviewer quota exhausted; switching to "
                      f"{review_rotation.name}.")
            continue
        if result.failure_kind == "billing":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output, result.failure_kind)
            print(f"Auto-fix reviewer billing/balance failure: {reason}")
            return finish(_AutoFixReviewOutcome(1, human_reason=reason))
        if result.failure_kind == "authentication":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            if review.writes:
                gitops.commit_if_dirty(
                    cfg.root,
                    f"wip({cfg.tasks_name}): plan reviewer authentication failover",
                    cfg.git_excludes)
            action = _authentication_failover_action(
                review_rotation, failed_adapters, reason)
            if action == "switch":
                print("Auto-fix reviewer authentication failure; switching to "
                      f"declared adapter {review_rotation.name}.")
            else:
                print("Auto-fix reviewer authentication failure; waiting only "
                      f"for recoverable adapter {review_rotation.name}.")
                _wait_for_rotation(cfg, sleep)
            continue
        if result.exit_code != 0 or result.stalled:
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output, result.failure_kind)
            print(f"Auto-fix reviewer adapter failure: {reason}")
            if result.exit_code == 130 or result.failure_kind == "interrupt":
                return finish(_AutoFixReviewOutcome(130, human_reason=reason))
            if _adapter_availability_failed(result):
                switched = review_rotation.advance_after_failure(
                    failed_adapters)
                if review.writes:
                    gitops.commit_if_dirty(
                        cfg.root,
                        f"wip({cfg.tasks_name}): plan reviewer adapter failover",
                        cfg.git_excludes)
                if switched:
                    print(f"Switching to declared adapter {review_rotation.name}.")
                else:
                    print("Every declared review adapter is unavailable; waiting "
                          f"{cfg.rotation_poll_minutes} minute(s) before "
                          f"restarting with {review_rotation.name}.")
                    _wait_for_rotation(cfg, sleep)
                continue
            return finish(_AutoFixReviewOutcome(1, human_reason=reason))

        review_output = (result.structured_output
                         if result.structured_output is not None
                         else result.output)
        try:
            if result.structured_output_error is not None:
                raise AssentError(result.structured_output_error)
            record = auto_fix.parse_review_output(review_output)
            if (review_context == "blocked_adjudication"
                    and record.verdict == "PASS" and blocked):
                raise AssentError(
                    "A blocked adjudication cannot PASS while a task remains BLOCKED")
            if pending_focused_failure and record.verdict == "PASS":
                raise AssentError(
                    "A focused_sweep failure cannot PASS until a later "
                    "focused_sweep succeeds")
            shared_decision = _validated_auto_fix_shared_paths_decision(
                cfg, review_shared_contract, shared_contract_after, record)
        except AssentError as e:
            diagnostic = _bounded_adapter_diagnostic(review_output)
            if invalid_attempts < cfg.retry_per_task:
                invalid_attempts += 1
                print(f"Auto-fix reviewer returned invalid output ({e}); retrying "
                      f"({invalid_attempts}/{cfg.retry_per_task}). "
                      f"bounded diagnostic: {diagnostic}")
                attempt_prompt = prompt + _auto_fix_review_correction(
                    str(e), diagnostic)
                continue
            print(f"Auto-fix reviewer returned invalid output after configured retries: {e}; "
                  f"bounded diagnostic: {diagnostic}")
            outcome = _AutoFixReviewOutcome(1, human_reason=str(e))
            if changed:
                outcome = _recover_invalid_reviewer_writes(
                    cfg, plan, outcome, now)
            return finish(outcome)

        try:
            resolved_record = auto_fix.validate_review_findings(record, plan)
            resolved_record = auto_fix.validate_review_transitions(
                resolved_record, review_stage=review_stage,
                previous=review_previous,
                repair_changed_paths=(
                    _auto_fix_changed_paths(cfg, review_previous.source_tree)
                    if review_stage == "recheck"
                    and review_previous is not None else None))
            current_additions = tuple(
                auto_fix.ApprovedScopeAddition(
                    auto_fix.finding_fingerprint(finding), finding.task_id,
                    finding.scope_addition.path,
                    finding.scope_addition.path_state)
                for finding in resolved_record.findings
                if finding.task_id is not None
                and finding.scope_addition is not None)
            if not review.writes and resolved_record.verdict == "FIXED":
                raise AssentError("A read-only verdict role cannot return FIXED")
            if current_additions:
                if review.writes and resolved_record.verdict != "FIXED":
                    raise AssentError(
                        "A write-capable verdict role must repair an approved "
                        "scope omission in the same session and return FIXED")
                auto_fix.validate_scope_additions(
                    cfg.root, plan, current_additions,
                    baseline_ref=(source_tree if review.writes else None),
                    materialized_new_files=review.writes)
                if review.writes:
                    changed_paths = {
                        path.replace("\\", "/")
                        for path in gitops.dirty_paths(
                            cfg.root, cfg.git_excludes)
                    }
                    missing_repairs = [
                        item.path for item in current_additions
                        if item.path not in changed_paths]
                    if missing_repairs:
                        raise AssentError(
                            "A same-session scope amendment did not repair its "
                            "exact path: " + ", ".join(missing_repairs))
            if changed and resolved_record.verdict != "FIXED":
                shown = ", ".join(changed[:8]) + (
                    " ..." if len(changed) > 8 else "")
                raise AssentError(
                    "A write-capable verdict role that changed source must return "
                    f"FIXED; changed paths: {shown}")
            if resolved_record.verdict == "FIXED" and not changed:
                raise AssentError(
                    "A write-capable verdict role returned FIXED without changing source")
            freshness.update(
                reviewer_adapter=session.agent,
                reviewer_model=session.requested_model,
                reviewer_effort=effort_identity(session.requested_effort))
            next_workflow_step = (
                initial_role_count
                if (initial_quality_review or rebound_review)
                and resolved_record.verdict == "PASS"
                else (round_index if resolved_record.verdict == "PASS"
                      else round_index + 1))
            state = auto_fix.state_for_review(
                resolved_record, previous=ledger_previous,
                review_stage=review_stage,
                workflow_step_index=next_workflow_step,
                **freshness)
            state = _auto_fix_attach_repair_briefs(
                cfg, plan, state,
                blocker_evidence=_auto_fix_blocker_text(blockers),
                focused_evidence="\n".join(focused_lines))
        except AssentError as e:
            if record.verdict in ("PASS", "FIXED"):
                print(f"Auto-fix reviewer verdict rejected: {e}")
                outcome = _AutoFixReviewOutcome(
                    1, ledger_previous, str(e))
                if changed:
                    outcome = _recover_invalid_reviewer_writes(
                        cfg, plan, outcome, now)
                return finish(outcome)
            # Preserve the reviewer's concrete output even though it cannot
            # authorize a write-capable task session.
            try:
                state = auto_fix.state_for_review(
                    record, previous=ledger_previous, review_stage=review_stage,
                    enforce_transitions=False, **freshness)
                auto_fix.write_auto_fix_state(
                    auto_fix.auto_fix_state_path(cfg), state)
            except AssentError as state_error:
                print("Auto-fix invalid reviewer evidence could not be encoded "
                      f"as repair state: {state_error}")
                return finish(_AutoFixReviewOutcome(1, human_reason=str(e)))
            print(f"Auto-fix findings require a human scope/plan decision: {e}")
            return finish(_AutoFixReviewOutcome(1, state, str(e)))

        if changed:
            outside = _auto_fix_out_of_scope_writes(
                cfg, plan, resolved_record,
                scope_additions=current_additions)
            if outside:
                shown = ", ".join(outside[:8]) + (
                    " ..." if len(outside) > 8 else "")
                print("The review round wrote outside the declared scope of the "
                      "task its finding names; the verdict was ignored and the "
                      f"exact edits are preserved for explicit human recovery "
                      f"({shown}).")
                return finish(_AutoFixReviewOutcome(
                    1, human_reason="review round wrote outside the named task scope"))

        try:
            if shared_decision is not None:
                main = gitops.main_worktree(cfg.root)
                paths, watch = shared_decision
                shared_paths.review(
                    main, cfg.root, paths=paths, watch=watch,
                    none=not paths)
            shared_contract_after = _shared_paths_contract(cfg)
            shared_refusal = shared_paths.closeout_refusal(shared_contract_after)
            if shared_refusal:
                raise AssentError(shared_refusal[:1].upper() + shared_refusal[1:])
        except AssentError as e:
            print(f"Auto-fix reviewer shared-path decision rejected: {e}")
            outcome = _AutoFixReviewOutcome(1, ledger_previous, str(e))
            if changed:
                outcome = _recover_invalid_reviewer_writes(
                    cfg, plan, outcome, now)
            return finish(outcome)

        if changed:
            if current_additions:
                # Persist the exact decision before the scheduler changes a task
                # contract. Recovery can then finish the same transaction without
                # opening a second fixer session.
                auto_fix.write_auto_fix_state(
                    auto_fix.auto_fix_state_path(cfg), state)
                state, plan, contracts_by_id = _apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts_by_id, now,
                    baseline_ref=source_tree, materialized_new_files=True)
            # Git state belongs to the scheduler alone, so the round's approved
            # in-scope repair is checkpointed here.  The next round then reviews
            # a clean worktree and can name the exact repaired paths.
            owner = next(
                (task for task in (
                    plan.get(item.task_id) for item in resolved_record.findings
                    if item.task_id is not None)
                 if task is not None), None)
            if owner is not None:
                gitops.commit_if_dirty(
                    cfg.root,
                    _checkpoint_subject(
                        cfg, "auto", owner,
                        f"review round {round_index + 1} repaired its own finding"),
                    cfg.git_excludes)

        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        if resolved_record.verdict == "PASS":
            if initial_quality_review or rebound_review:
                print("Plan quality review: PASS; proceeding to focused_sweep.")
                return finish(_run_auto_fix_review_once(
                    cfg, once=once, task_id=task_id,
                    injected_adapter=injected_adapter, sleep=sleep, now=now,
                    blockers=blockers, trusted_plan=trusted_plan,
                    trusted_contracts=trusted_contracts,
                    gate_passes=gate_passes))
            print("Auto-fix plan review: PASS.")
            return finish(_AutoFixReviewOutcome(0, state))
        if resolved_record.verdict == "FIXED":
            print("Auto-fix plan review: FIXED; the round repaired what it found "
                  "and the next round confirms the result.")
        else:
            print("Auto-fix plan review: FAIL; blocking findings were preserved for repair.")
        for finding in resolved_record.findings:
            label = f"{finding.task_id}: " if finding.task_id else ""
            print(f"  - {label}{finding.path}: {finding.summary}")
        return finish(_AutoFixReviewOutcome(1, state))


def _auto_fix_forbidden_writes(changed: tuple[str, ...]) -> list[str]:
    """The review-interval writes no round may make, whatever its verdict.

    A merged reviewer-fixer round repairs ordinary source files; the management
    plane, the primary worktree, and Git state itself stay off limits exactly as
    they are for a worker session.  A dirty source worktree is the expected
    witness of an in-scope repair and is judged by scope, not here.
    """
    return [item for item in changed
            if not item.startswith("source:") or item == "source:.git/HEAD"]


def _auto_fix_out_of_scope_writes(
        cfg: Config, plan: Plan,
        record: auto_fix.ReviewRecord, *,
        scope_additions: tuple[auto_fix.ApprovedScopeAddition, ...] = (),
        since_ref: str | None = None) -> list[str]:
    """Uncommitted round writes outside the declared scope of the named tasks.

    Repair is authorized exactly as far as the one existing task a finding
    names, so this is the same ``gitops.changes_outside_scope`` floor
    ``_inspect_task_safety`` applies to an ordinary worker session.  A round
    that asserts nothing is wrong names no task, and an empty scope already
    fails closed, so such a round is authorized to write nothing at all.
    """
    scope: list[str] = []
    for finding in record.findings:
        task = plan.get(finding.task_id) if finding.task_id else None
        if task is not None:
            scope.extend(task.scope)
    scope.extend(item.path for item in scope_additions)
    return gitops.changes_outside_scope(
        cfg.root, list(dict.fromkeys(scope)), since_ref=since_ref,
        excludes=cfg.git_excludes)


def _recover_invalid_reviewer_writes(
        cfg: Config, plan: Plan, outcome: _AutoFixReviewOutcome,
        now: Callable[[], datetime]) -> _AutoFixReviewOutcome:
    """Checkpoint terminal malformed-output writes only for one proven owner."""
    if gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        print("Invalid reviewer output changed project surfaces but left no "
              "checkpointable source path; retained for explicit human recovery.")
        return _AutoFixReviewOutcome(
            outcome.code, outcome.state,
            "invalid reviewer writes require explicit human recovery")

    owners = [
        task for task in plan.tasks
        if not gitops.changes_outside_scope(
            cfg.root, task.scope, excludes=_task_excludes(cfg, task))
    ]
    if len(owners) != 1:
        reason = ("no task uniquely contains every preserved path"
                  if not owners else
                  "more than one task could own every preserved path")
        print("Invalid reviewer output left source writes that cannot be "
              f"attributed safely ({reason}); edits are retained for explicit "
              "human recovery.")
        return _AutoFixReviewOutcome(
            outcome.code, outcome.state,
            "invalid reviewer writes require explicit human recovery")

    owner = owners[0]
    try:
        committed = gitops.commit_if_dirty(
            cfg.root,
            _checkpoint_subject(
                cfg, "wip", owner,
                "recovered invalid reviewer writes, scope-verified"),
            cfg.git_excludes)
    except (AssentError, OSError) as e:
        print("Invalid reviewer source writes were scope-verified against "
              f"{owner.id}, but the WIP checkpoint failed: {e}; edits are "
              "retained for explicit human recovery.")
        return _AutoFixReviewOutcome(
            outcome.code, outcome.state,
            "invalid reviewer writes could not be checkpointed")
    if not committed:
        print("Invalid reviewer source writes were scope-verified but no WIP "
              "checkpoint could be created; retained for explicit human recovery.")
        return _AutoFixReviewOutcome(
            outcome.code, outcome.state,
            "invalid reviewer writes could not be checkpointed")

    try:
        append_entry(
            owner.journal_path, by="scheduler",
            event="auto_fix_invalid_output_recovery",
            summary=("Recovered invalid reviewer writes; scope-verified against "
                     f"{owner.id} and kept in a WIP checkpoint"),
            detail=("The write-capable reviewer exhausted its invalid-output "
                    "retries after changing source. Every preserved path was "
                    "mechanically contained by this task's declared scope; "
                    "the scheduler kept its already-proven status and did not "
                    "advance the review cursor."),
            time_str=now().isoformat(timespec="seconds"))
    except (AssentError, OSError) as e:
        print("The invalid-reviewer WIP checkpoint was created, but scheduler "
              f"recovery evidence could not be journaled: {e}")
    print("Invalid reviewer source writes were scope-verified against "
          f"{owner.id} and kept in a WIP checkpoint; the task status and "
          "review cursor were not advanced.")
    return outcome


def _auto_fix_profile_for_task(cfg: Config, task: Task) -> _FixerProfile:
    """The primary worker's ordinary identity for one reopened task."""
    return _FixerProfile(cfg.adapter_names, task.model, task.effort)


def _auto_fix_adapter(
        rotation: _AdapterRotation,
        adapter_name: str) -> Adapter:
    try:
        return rotation.adapters[rotation.names.index(adapter_name)]
    except ValueError as e:
        raise AssentError(
            f"Auto-fix profile names an adapter outside the worker rotation: "
            f"{adapter_name}") from e


def _fixer_rotation_and_sessions(
        cfg: Config, profile: _FixerProfile,
        worker_rotation: _AdapterRotation | None = None
        ) -> tuple[_AdapterRotation, dict[str, SessionIdentity], list[str]]:
    """Resolve and preflight every declared fallback for one fixer profile."""
    if has_literal(profile.model, profile.effort) and len(profile.adapters) != 1:
        return (_AdapterRotation((), ()), {},
                ["a literal model or effort must resolve to exactly one adapter"])
    adapters: list[Adapter] = []
    names: list[str] = []
    sessions: dict[str, SessionIdentity] = {}
    errors: list[str] = []
    for name in profile.adapters:
        adapter = (_auto_fix_adapter(worker_rotation, name)
                   if worker_rotation is not None
                   and name in worker_rotation.names
                   else get_adapter(name, cfg))
        effort = cfg.adapter_settings(name).resolve_effort(
            profile.effort, profile.model)
        if effort is None and literal_value(profile.model) is None:
            errors.append(f"{name}: no concrete effort")
            continue
        session, candidate_errors = auto_fix_fixer_capability_errors(
            cfg, adapter, name, profile.model, effort)
        if session is not None:
            sessions[name] = session
        errors.extend(f"{name}: {message}" for message in candidate_errors)
        names.append(name)
        adapters.append(adapter)
    return _AdapterRotation(tuple(names), tuple(adapters)), sessions, errors


def _auto_fix_task_diff(cfg: Config, task: Task) -> str:
    """Render the bounded cumulative source delta relevant to one task."""
    base = resolve_stack_state(cfg).base.resolved_base
    merge_base = gitops.merge_base(cfg.root, base, "HEAD")
    result = subprocess.run(
        ["git", "diff", "--find-renames", f"{merge_base}..HEAD", "--",
         *task.scope], cwd=str(cfg.root), capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode == 0:
        diff = result.stdout or "(no committed diff in this task's scope)"
    else:
        diff = ("(unable to render diff: "
                f"{_bounded_adapter_diagnostic(result.stderr or result.stdout)})")
    if len(diff) > _AUTO_FIX_EVIDENCE_LIMIT:
        diff = diff[:_AUTO_FIX_EVIDENCE_LIMIT] + "\n... [diff truncated]"
    return diff.rstrip()


def _auto_fix_repair_briefs(
        cfg: Config, plan: Plan, state: auto_fix.AutoFixState, *,
        blocker_evidence: str, focused_evidence: str,
        ) -> tuple[auto_fix.RepairBrief, ...]:
    """Build the exact durable reviewer-to-worker handoff for this decision."""
    current = set(state.current_finding_fingerprints)
    finding_lines: list[str] = []
    for finding in state.findings:
        if finding.fingerprint not in current:
            continue
        addition = "none"
        if finding.scope_addition_path is not None:
            addition = (
                f"{finding.scope_addition_path} "
                f"({finding.scope_addition_path_state})")
        finding_lines.append(
            f"- fingerprint: {finding.fingerprint}\n"
            f"  kind: {finding.kind}\n"
            f"  owner: {finding.task_id or 'unassigned'}\n"
            f"  path: {finding.path}\n"
            f"  problem: {finding.summary}\n"
            f"  evidence: {finding.evidence}\n"
            f"  reviewer recommendation: {finding.recommendation}\n"
            f"  approved scope addition: {addition}")
    findings = "\n".join(finding_lines) or "- none"
    additions = "\n".join(
        f"- {item.fingerprint} {item.task_id}: {item.path} ({item.path_state})"
        for item in state.approved_scope_additions) or "- none"
    fingerprints = state.current_finding_fingerprints
    briefs = []
    implicated = list(dict.fromkeys(
        finding.task_id for finding in state.findings
        if finding.fingerprint in current and finding.task_id is not None))
    for task in _auto_fix_cascade_tasks(plan, implicated):
        brief = (
            f"Task: {task.id}\n"
            f"Current findings:\n{findings}\n\n"
            "Original blocker evidence:\n"
            f"{blocker_evidence or '(none)'}\n\n"
            "Focused command evidence:\n"
            f"{focused_evidence or '(none)'}\n\n"
            f"Approved scope additions:\n{additions}\n\n"
            "Relevant cumulative diff:\n"
            f"{_auto_fix_task_diff(cfg, task)}"
        )
        briefs.append(auto_fix.RepairBrief(task.id, fingerprints, brief))
    return tuple(briefs)


def _auto_fix_prior_brief_evidence(
        state: auto_fix.AutoFixState, heading: str,
        next_heading: str) -> str | None:
    """Recover one scheduler-rendered evidence section from durable briefs."""
    marker = heading + ":\n"
    ending = "\n\n" + next_heading + ":\n"
    recovered: set[str] = set()
    for item in state.repair_briefs:
        start = item.brief.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = item.brief.find(ending, start)
        if end >= 0:
            recovered.add(item.brief[start:end])
    if len(recovered) > 1:
        raise AssentError(
            f"Auto-fix durable repair briefs disagree on {heading.lower()}")
    return next(iter(recovered), None)


def _auto_fix_merge_brief_evidence(
        prior: str | None, current: str, *, empty: str) -> str:
    """Retain original evidence while adding a distinct later gate result."""
    if prior is None:
        return current
    if not current or current == empty:
        return prior
    if current in prior:
        return prior
    return prior + "\n\nLater repair-round evidence:\n" + current


def _auto_fix_attach_repair_briefs(
        cfg: Config, plan: Plan, state: auto_fix.AutoFixState, *,
        blocker_evidence: str, focused_evidence: str) -> auto_fix.AutoFixState:
    if state.verdict != "FAIL":
        return state
    prior_blocker = _auto_fix_prior_brief_evidence(
        state, "Original blocker evidence", "Focused command evidence")
    prior_focused = _auto_fix_prior_brief_evidence(
        state, "Focused command evidence", "Approved scope additions")
    blocker_evidence = _auto_fix_merge_brief_evidence(
        prior_blocker, blocker_evidence,
        empty="(none; this is a completed-plan review)")
    focused_evidence = _auto_fix_merge_brief_evidence(
        prior_focused, focused_evidence, empty="")
    return auto_fix.with_repair_briefs(
        state, _auto_fix_repair_briefs(
            cfg, plan, state, blocker_evidence=blocker_evidence,
            focused_evidence=focused_evidence))


def _auto_fix_repair_context(
        task: Task, state: auto_fix.AutoFixState) -> str:
    """Inject the exact durable brief; never reconstruct it from memory."""
    matches = [item for item in state.repair_briefs if item.task_id == task.id]
    if len(matches) != 1:
        raise AssentError(
            f"Auto-fix state has no exact durable repair brief for {task.id}")
    brief = matches[0]
    if brief.finding_fingerprints != state.current_finding_fingerprints:
        raise AssentError(
            f"Auto-fix durable repair brief for {task.id} is stale")
    return _AUTO_FIX_REPAIR_SUFFIX.format(repair_brief=brief.brief)


def _auto_fix_failure_state(
        cfg: Config, state: auto_fix.AutoFixState,
        failures: list[tuple[Task, str]]) -> auto_fix.AutoFixState:
    """Join concrete ordinary-gate failures to the same durable ledger."""
    record = auto_fix.ReviewRecord("FAIL", tuple(
        auto_fix.ReviewFinding(
            task.id, auto_fix.scheduler_finding_path(task.scope[0]),
            "Automatic repair task gate failed", reason)
        for task, reason in failures))
    plan = Plan.parse(cfg.tasks_dir)
    record = auto_fix.validate_review_findings(record, plan)
    next_state = auto_fix.state_for_review(
        record, previous=state,
        source_tree=gitops.tree_of(cfg.root, "HEAD"),
        task_plan_sha256=auto_fix.sha256_files(
            task.path for task in plan.tasks),
        review_prompt_sha256=state.review_prompt_sha256,
        reviewer_role=state.reviewer_role,
        reviewer_step_index=state.reviewer_step_index,
        reviewer_adapter=state.reviewer_adapter,
        reviewer_model=state.reviewer_model,
        reviewer_effort=state.reviewer_effort)
    auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), next_state)
    return next_state


_SCOPE_AMENDMENT_EVENT = "auto_fix_scope_amendment"
_SCOPE_AMENDMENT_SUMMARY = (
    "Scheduler appended reviewer-approved exact task scope paths")


def _scope_amendment_payload(
        additions: list[auto_fix.ApprovedScopeAddition]) -> tuple[str, str]:
    fingerprints = json.dumps(
        [item.fingerprint for item in additions], separators=(",", ":"))
    delta = json.dumps(
        [{"path": item.path, "path_state": item.path_state}
         for item in additions], sort_keys=True, separators=(",", ":"))
    return fingerprints, delta


def _scope_amendment_additions(
        amendment: auto_fix.ScopeAmendment,
        ) -> list[auto_fix.ApprovedScopeAddition]:
    return [
        auto_fix.ApprovedScopeAddition(fingerprint, amendment.task_id, path, state)
        for fingerprint, path, state in zip(
            amendment.finding_fingerprints, amendment.paths,
            amendment.path_states)
    ]


def _scope_amendment_was_journaled(
        task: Task, amendment: auto_fix.ScopeAmendment) -> bool:
    """Recognize the exact durable scheduler transaction in the journal."""
    try:
        entries = read_entries(task.journal_path)
    except (AssentError, OSError, ValueError):
        return False
    detail = _scope_amendment_detail(
        _scope_amendment_additions(amendment),
        task_before=amendment.task_before_sha256,
        task_after=amendment.task_after_sha256,
        plan_before=amendment.plan_before_sha256,
        plan_after=amendment.plan_after_sha256)
    return any(
        entry.get("by") == "scheduler"
        and entry.get("event") == _SCOPE_AMENDMENT_EVENT
        and entry.get("summary") == _SCOPE_AMENDMENT_SUMMARY
        and entry.get("detail") in {detail, detail + "\n"}
        for entry in entries)


def _scope_amendment_detail(
        additions: list[auto_fix.ApprovedScopeAddition], *,
        task_before: str, task_after: str,
        plan_before: str, plan_after: str) -> str:
    fingerprints, delta = _scope_amendment_payload(additions)
    return (
        f"finding fingerprints: {fingerprints}\n"
        f"scope delta: {delta}\n"
        f"task contract before sha256: {task_before}\n"
        f"task contract after sha256: {task_after}\n"
        f"task plan before sha256: {plan_before}\n"
        f"task plan after sha256: {plan_after}\n"
        "authorization: configured workflow reviewer decision"
    )


def _ensure_scope_amendment_writable(
        state_path, tasks: list[Task]) -> None:
    """Refuse the whole scope transaction before its first mutation."""
    targets = [state_path]
    for task in tasks:
        targets.extend((task.path, task.journal_path))
    for target in targets:
        writable = target if target.exists() else target.parent
        if not os.access(writable, os.W_OK):
            raise AssentError(
                f"Auto-fix scope amendment target is not writable: {target}")
    for task in tasks:
        read_entries(task.journal_path)


def _apply_reviewed_scope_amendments(
        cfg: Config, state: auto_fix.AutoFixState,
        plan: Plan, contracts_by_id: dict[str, str],
        now: Callable[[], datetime],
        *, baseline_ref: str | None = None,
        materialized_new_files: bool | None = None,
        ) -> tuple[auto_fix.AutoFixState, Plan, dict[str, str]]:
    """Complete or resume the scheduler-owned amendment before ordinary rework."""
    if not state.approved_scope_additions:
        return state, plan, contracts_by_id

    disk_plan = Plan.parse(cfg.tasks_dir)
    disk_tasks = {task.id: task for task in disk_plan.tasks}
    for addition in state.approved_scope_additions:
        if addition.task_id not in disk_tasks:
            raise AssentError(
                f"Auto-fix scope amendment task disappeared: {addition.task_id}")

    current_contracts = _task_contract_snapshots(disk_plan)
    recorded = {
        fingerprint
        for amendment in state.scope_amendments
        for fingerprint in amendment.finding_fingerprints}
    unrecorded = [
        item for item in state.approved_scope_additions
        if item.fingerprint not in recorded]
    if unrecorded:
        grouped: dict[str, list[auto_fix.ApprovedScopeAddition]] = {}
        for addition in unrecorded:
            grouped.setdefault(addition.task_id, []).append(addition)
        materialized = (state.verdict == "FIXED" if materialized_new_files is None
                        else materialized_new_files)
        validation_ref = (state.source_tree if baseline_ref is None and materialized
                          else baseline_ref)
        auto_fix.validate_scope_additions(
            cfg.root, disk_plan, unrecorded, baseline_ref=validation_ref,
            materialized_new_files=materialized)
        affected = [disk_tasks[task_id] for task_id in grouped]
        _ensure_scope_amendment_writable(
            auto_fix.auto_fix_state_path(cfg), affected)

        before_digest = _contracts_digest(disk_plan, current_contracts)
        if before_digest != state.task_plan_sha256:
            raise AssentError(
                "Task plan drifted before the reviewed scope amendment")
        after_contracts = dict(current_contracts)
        after_tasks: list[Task] = []
        for task in disk_plan.tasks:
            additions = grouped.get(task.id, ())
            if not additions:
                after_tasks.append(task)
                continue
            paths = [item.path for item in additions]
            after_contracts[task.id] = scope_text_with_entries(
                current_contracts[task.id], paths)
            after_tasks.append(replace(task, scope=task.scope + paths))
        after_plan = Plan(after_tasks, disk_plan.dir)
        after_digest = _contracts_digest(after_plan, after_contracts)
        amendments = list(state.scope_amendments)
        for task_id, additions in grouped.items():
            amendments.append(auto_fix.ScopeAmendment(
                finding_fingerprints=tuple(
                    item.fingerprint for item in additions),
                task_id=task_id,
                paths=tuple(item.path for item in additions),
                path_states=tuple(item.path_state for item in additions),
                task_before_sha256=task_text_sha256(
                    current_contracts[task_id]),
                task_after_sha256=task_text_sha256(after_contracts[task_id]),
                plan_before_sha256=before_digest,
                plan_after_sha256=after_digest))
        state = auto_fix.with_scope_amendments(state, tuple(amendments))
        # This is the durable transaction boundary.  It records the exact
        # decision, delta, and before/after identities before any task changes.
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)

    incomplete = [
        amendment for amendment in state.scope_amendments
        if not _scope_amendment_was_journaled(
            disk_tasks[amendment.task_id], amendment)]
    if not incomplete:
        return state, disk_plan, current_contracts

    pairs = list(dict.fromkeys(
        (item.plan_before_sha256, item.plan_after_sha256)
        for item in incomplete))
    for plan_before, plan_after in pairs:
        transaction = [
            item for item in state.scope_amendments
            if (item.plan_before_sha256, item.plan_after_sha256)
            == (plan_before, plan_after)]
        transaction_tasks = {item.task_id for item in transaction}
        if len(transaction_tasks) != len(transaction):
            raise AssentError(
                "Auto-fix scope transaction contains duplicate task records")
        affected = [disk_tasks[task_id] for task_id in transaction_tasks]
        _ensure_scope_amendment_writable(
            auto_fix.auto_fix_state_path(cfg), affected)

        live_plan = Plan.parse(cfg.tasks_dir)
        live_contracts = _task_contract_snapshots(live_plan)
        pre_contracts = dict(live_contracts)
        post_contracts = dict(live_contracts)
        pre_tasks: list[Task] = []
        post_tasks: list[Task] = []
        by_task = {item.task_id: item for item in transaction}
        applied: set[str] = set()
        all_additions: list[auto_fix.ApprovedScopeAddition] = []
        for task in live_plan.tasks:
            amendment = by_task.get(task.id)
            if amendment is None:
                pre_tasks.append(task)
                post_tasks.append(task)
                continue
            all_additions.extend(_scope_amendment_additions(amendment))
            text = live_contracts[task.id]
            digest = task_text_sha256(text)
            paths = list(amendment.paths)
            if digest == amendment.task_before_sha256:
                if any(path in task.scope for path in paths):
                    raise AssentError(
                        f"Unapplied scope amendment is partially present in {task.id}")
                after_text = scope_text_with_entries(text, paths)
                if task_text_sha256(after_text) != amendment.task_after_sha256:
                    raise AssentError(
                        f"Precomputed scope amendment no longer matches {task.id}")
                post_contracts[task.id] = after_text
                pre_tasks.append(task)
                post_tasks.append(replace(task, scope=task.scope + paths))
            elif digest == amendment.task_after_sha256:
                if task.scope[-len(paths):] != paths:
                    raise AssentError(
                        f"Applied scope amendment is not the exact suffix of {task.id}")
                before_text = scope_text_without_entries(text, paths)
                if task_text_sha256(before_text) != amendment.task_before_sha256:
                    raise AssentError(
                        f"Applied scope amendment no longer matches {task.id}")
                pre_contracts[task.id] = before_text
                pre_tasks.append(replace(task, scope=task.scope[:-len(paths)]))
                post_tasks.append(task)
                applied.add(task.id)
            else:
                raise AssentError(
                    f"Task contract drifted across the scope amendment: {task.id}")

        pre_plan = Plan(pre_tasks, live_plan.dir)
        post_plan = Plan(post_tasks, live_plan.dir)
        materialized = (state.verdict == "FIXED" if materialized_new_files is None
                        else materialized_new_files)
        validation_ref = (state.source_tree if baseline_ref is None and materialized
                          else baseline_ref)
        auto_fix.validate_scope_additions(
            cfg.root, pre_plan, all_additions, baseline_ref=validation_ref,
            materialized_new_files=materialized)
        if _contracts_digest(pre_plan, pre_contracts) != plan_before:
            raise AssentError(
                "Scope amendment pre-plan does not match its durable digest")
        if _contracts_digest(post_plan, post_contracts) != plan_after:
            raise AssentError(
                "Scope amendment post-plan does not match its durable digest")
        if state.task_plan_sha256 not in {plan_before, plan_after}:
            raise AssentError(
                "Auto-fix state is outside the durable scope plan transition")

        for amendment in transaction:
            if amendment.task_id in applied:
                continue
            add_scope_entries(
                disk_tasks[amendment.task_id].path, list(amendment.paths),
                expected_sha256=amendment.task_before_sha256)

        amended_plan = Plan.parse(cfg.tasks_dir)
        amended_contracts = _task_contract_snapshots(amended_plan)
        if _contracts_digest(amended_plan, amended_contracts) != plan_after:
            raise AssentError(
                "Scope amendment result does not match its durable plan digest")
        if state.task_plan_sha256 == plan_before:
            state = auto_fix.with_plan_digest_transition(
                state, plan_before, plan_after)
            auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        elif not any(
                item.before_sha256 == plan_before
                and item.after_sha256 == plan_after
                for item in state.plan_digest_transitions):
            raise AssentError(
                "Applied scope amendment is missing its durable plan transition")

        for amendment in transaction:
            task = amended_plan.get(amendment.task_id)
            assert task is not None
            if _scope_amendment_was_journaled(task, amendment):
                continue
            detail = _scope_amendment_detail(
                _scope_amendment_additions(amendment),
                task_before=amendment.task_before_sha256,
                task_after=amendment.task_after_sha256,
                plan_before=amendment.plan_before_sha256,
                plan_after=amendment.plan_after_sha256)
            append_entry(
                task.journal_path, by="scheduler",
                event=_SCOPE_AMENDMENT_EVENT,
                summary=_SCOPE_AMENDMENT_SUMMARY, detail=detail,
                time_str=now().isoformat(timespec="seconds"))

        disk_plan = Plan.parse(cfg.tasks_dir)
        disk_tasks = {task.id: task for task in disk_plan.tasks}

    final_plan = Plan.parse(cfg.tasks_dir)
    return state, final_plan, _task_contract_snapshots(final_plan)


def _auto_fix_cascade_tasks(plan: Plan, implicated: list[str]) -> list[Task]:
    """Return the finite automatic-rework dependency closure in plan order."""
    selected = set(implicated)
    changed = True
    while changed:
        changed = False
        for task in plan.tasks:
            if task.id in selected or not any(dep in selected for dep in task.deps):
                continue
            selected.add(task.id)
            changed = True
    return [task for task in plan.tasks
            if task.id in selected and task.status != "SKIP"]


def _auto_fix_finish_rounds_exhausted(
        cfg: Config, state: auto_fix.AutoFixState,
        now: Callable[[], datetime], *,
        gate_passes: _FocusedGateLedger | None = None) -> int:
    """The finite review-round sequence ran out; stop without another round.

    Nothing is reverted, reopened, or re-marked: the durable finding ledger,
    every edit a round made, and each task's own status stay exactly as the
    last round left them.  A sequence that ended on a round which repaired what
    it found (verdict ``FIXED``) is not a failure -- the code passed every
    focused gate its own tasks declare and only independent review confirmation
    is missing -- so it settles as the distinct, human-gated SELF-FIXED,
    UNREVIEWED outcome and the run succeeds.  That claim is what the settling
    gate below proves: the last round wrote its repair after those gates last
    ran, so they run once more on the repaired source before anything settles,
    and a repair that breaks one ends the run nonzero instead of handing a
    human an unproven outcome.  A blocker no round repaired is likewise not an
    infrastructure failure but a question the scheduler cannot decide, so it
    settles as the distinct, human-gated REVIEW UNRESOLVED outcome and the run
    also succeeds; only a broken gate keeps a nonzero exit here.
    """
    if state.verdict != "FIXED":
        return _auto_fix_settle_unresolved_review(cfg, state, now)
    already_settled = state.self_fixed_unreviewed is not None
    gate_passed, evidence, gate_tasks = (
        (True, "", ()) if already_settled
        else _auto_fix_settling_gates(cfg, state, gate_passes, now))
    if not already_settled:
        state = _auto_fix_with_settling_gate_evidence(
            state, gate_tasks, evidence)
    try:
        # The durable outcome and the report a human reads must both be on disk
        # before this folder hands control back, so the report refresh follows
        # the same unconditional discipline folder verification's own closeout
        # uses -- a later failure can never leave either unwritten.  A failed
        # settling gate takes the same discipline: its evidence is durable, and
        # only the settled outcome itself is withheld.
        if not gate_passed:
            # The preserved state is rebound to the tree the gate was just
            # proven against, exactly as both settle branches below rebind
            # theirs: the final round's repair was checkpointed after the last
            # review bound this record, so writing it unchanged would make the
            # report call the freshest evidence the folder has STALE (source
            # tree changed) instead of the pending FAILED (fresh) verdict plus
            # the failing gate's command and evidence.
            current_tree = _auto_fix_current_tree(cfg)
            if current_tree is not None:
                state = replace(state, source_tree=current_tree)
            auto_fix.write_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg), state)
            print(f"Auto-fix review rounds exhausted after "
                  f"{state.workflow_step_index} workflow step(s); the final step's "
                  "repair was not proven by the focused gate of the task it "
                  "repaired, so the folder did not settle. Every finding, "
                  "edit, and journal entry was preserved without another "
                  "round.")
            return 1
        settled = auto_fix.with_self_fixed_unreviewed(
            state, source_tree=_auto_fix_current_tree(cfg))
        outcome = settled.self_fixed_unreviewed
        assert outcome is not None
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg), settled)
        _print_self_fixed_unreviewed(outcome)
        if not already_settled:
            _auto_fix_journal_self_fixed(
                cfg, settled, now, gate_evidence=evidence)
    finally:
        try_write_report(cfg)
    return 0


def _auto_fix_settle_unresolved_review(
        cfg: Config, state: auto_fix.AutoFixState,
        now: Callable[[], datetime]) -> int:
    """Hand an unresolved finding to the human meeting instead of failing the run.

    The configured rounds ran out with a blocker none of them repaired.  That is
    a question the scheduler cannot settle, not an infrastructure failure, a
    refused precondition, or a broken gate, so it must not exit nonzero: a
    nonzero folder stops the launch loop and silently cancels every unrelated
    folder still queued behind it in the same invocation.  The outcome instead
    becomes a durable terminal record plus a distinctly named report state, and
    the explicit ``accept`` gate stays the human decision point.  Every task
    keeps the status its own closeout gave it; nothing is reverted, reopened, or
    marked BLOCKED.
    """
    already_settled = state.unresolved_review is not None
    try:
        # The durable outcome and the report a human reads must both be on disk
        # before this folder hands control back, under the same unconditional
        # discipline the self-fixed settlement above uses.
        settled = auto_fix.with_unresolved_review(
            state, source_tree=_auto_fix_current_tree(cfg))
        outcome = settled.unresolved_review
        assert outcome is not None
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg), settled)
        _print_unresolved_review(outcome)
        if not already_settled:
            _auto_fix_journal_unresolved_review(cfg, settled, now)
    finally:
        try_write_report(cfg)
    return 0


def _print_unresolved_review(
        outcome: auto_fix.UnresolvedReviewOutcome) -> None:
    print(f"Auto-fix plan review: REVIEW UNRESOLVED, HUMAN DECISION after "
          f"{outcome.rounds_used} configured round(s); review round "
          f"{outcome.round_index + 1} "
          f"({outcome.adapter}/{outcome.model}/{outcome.effort}) left "
          f"{len(outcome.finding_fingerprints)} finding(s) no round resolved. "
          "Every task keeps the status its own closeout gave it, and the "
          "findings, edits and journals are preserved for the human acceptance "
          "decision.")


def _auto_fix_journal_unresolved_review(
        cfg: Config, state: auto_fix.AutoFixState,
        now: Callable[[], datetime]) -> None:
    """Record the settled outcome once on every implicated task's r file."""
    outcome = state.unresolved_review
    assert outcome is not None
    ledger = {item.fingerprint: item for item in state.findings}
    by_task: dict[str, list[str]] = {}
    for fingerprint in outcome.finding_fingerprints:
        finding = ledger[fingerprint]
        if finding.task_id is None:
            continue
        by_task.setdefault(finding.task_id, []).append(
            f"- {fingerprint} {finding.path}: {finding.summary}")
    plan = Plan.parse(cfg.tasks_dir)
    identity = f"{outcome.adapter}/{outcome.model}/{outcome.effort}"
    for task_id, findings in by_task.items():
        task = plan.get(task_id)
        if task is None:
            continue
        append_entry(
            task.journal_path, by="scheduler",
            event="auto_fix_unresolved_review",
            summary=(f"Review round {outcome.round_index + 1} of "
                     f"{outcome.rounds_used} ({identity}) ended the configured "
                     "round list with this task's finding still unresolved"),
            detail=("The bounded review-and-repair loop is finitely over, so "
                    "the folder is REVIEW UNRESOLVED, HUMAN DECISION: the run "
                    "succeeds so unrelated queued folders still start, the task "
                    "keeps the status its own closeout gave it, and nothing was "
                    "reopened, reverted, or marked BLOCKED. The unresolved "
                    "findings are evidence for the human acceptance meeting.\n"
                    "Unresolved findings:\n" + "\n".join(findings)),
            time_str=now().isoformat(timespec="seconds"))


# The one heading the settling gate writes into a durable brief, so re-proving
# a folder replaces its own section instead of stacking another copy.
_AUTO_FIX_SETTLING_GATE_HEADING = "Settling focused gate evidence"


def _auto_fix_settling_gates(
        cfg: Config, state: auto_fix.AutoFixState,
        gate_passes: _FocusedGateLedger | None,
        now: Callable[[], datetime]) -> tuple[bool, str, tuple[str, ...]]:
    """Prove the last round's repair against the implicated tasks' own gates.

    SELF-FIXED, UNREVIEWED tells a human that every task passed the focused
    gate it declares itself, and that only independent review confirmation is
    missing.  The final round writes its repair after those gates last ran, so
    the statement is true of the repair only once they run again on it.  This
    reuses the ordinary focused-gate execution and the invocation's own
    ``_FocusedGateLedger``, so a command already proven against exactly this
    source is not run a second time.
    """
    ledger = {item.fingerprint: item for item in state.findings}
    implicated = tuple(dict.fromkeys(
        ledger[fingerprint].task_id
        for fingerprint in state.current_finding_fingerprints
        if ledger[fingerprint].task_id is not None))
    lines = [f"{_AUTO_FIX_SETTLING_GATE_HEADING} "
             f"({now().isoformat(timespec='seconds')}):"]

    def refused(line: str) -> tuple[bool, str, tuple[str, ...]]:
        lines.append(line)
        return False, "\n".join(lines), implicated

    if not implicated:
        return refused("- FAIL: no existing task owns the settling findings, "
                       "so no focused gate can prove the repair")
    plan = Plan.parse(cfg.tasks_dir)
    owners: dict[str, list[str]] = {}
    for task_id in implicated:
        task = plan.get(task_id)
        if task is None:
            return refused(f"- FAIL {task_id}: the repaired task is no longer "
                           "in the plan")
        owners.setdefault(task.verify, []).append(task.id)

    print("Auto-fix plan review: proving the final round's repair against "
          "the focused gates of the tasks it repaired.")
    reused = {command: gate_passes is not None
              and gate_passes.reusable(cfg, command)
              for command in owners}
    merged = _merged_unittest_passes(
        cfg, [command for command in owners if not reused[command]])
    for command, tasks in owners.items():
        label = ", ".join(tasks)
        if reused[command]:
            print(f"  verify: {command}")
            print("  reused authoritative PASS (scheduler gate, exit 0)")
            lines.append(
                f"- PASS (reused authoritative PASS) {label}: {command}")
            continue
        result = merged.get(command)
        if result is None:
            result = _verify_subprocess(cfg, command)
        _show_verify_result(command, result)
        if result.returncode != 0:
            diagnostic = _bounded_adapter_diagnostic(
                result.stderr or result.stdout or "")
            return refused(f"- FAIL ({result.returncode}) {label}: {command}; "
                           f"{diagnostic}")
        lines.append(f"- PASS {label}: {command}")
    return True, "\n".join(lines), implicated


def _auto_fix_with_settling_gate_evidence(
        state: auto_fix.AutoFixState, task_ids: tuple[str, ...],
        evidence: str) -> auto_fix.AutoFixState:
    """Persist the settling gate result where the folder report already reads.

    The durable repair brief is the folder's one free-text per-task evidence
    record, and the report renders its opening, so the gate result leads it: a
    human reading `_report.md` sees which command proved the final repair, and
    when, without opening the derived state file.
    """
    briefs: list[auto_fix.RepairBrief] = []
    carried: set[str] = set()
    for item in state.repair_briefs:
        brief = _auto_fix_without_gate_evidence(item.brief)
        if item.task_id in task_ids:
            carried.add(item.task_id)
            brief = f"{evidence}\n\n{brief}" if brief else evidence
        briefs.append(auto_fix.RepairBrief(
            item.task_id, item.finding_fingerprints, brief))
    briefs.extend(
        auto_fix.RepairBrief(
            task_id, state.current_finding_fingerprints, evidence)
        for task_id in task_ids if task_id not in carried)
    return auto_fix.with_repair_briefs(state, tuple(briefs))


def _auto_fix_without_gate_evidence(brief: str) -> str:
    """Drop an earlier settling-gate section so re-proving replaces it."""
    if not brief.startswith(_AUTO_FIX_SETTLING_GATE_HEADING):
        return brief
    return brief.partition("\n\n")[2]


def _auto_fix_current_tree(cfg: Config) -> str | None:
    """The tree the last round's repair was checkpointed into, when readable."""
    try:
        return gitops.tree_of(cfg.root, "HEAD")
    except AssentError:
        return None


def _print_self_fixed_unreviewed(outcome: auto_fix.SelfFixedOutcome) -> None:
    print(f"Auto-fix plan review: SELF-FIXED, UNREVIEWED after "
          f"{outcome.rounds_used} configured round(s); review round "
          f"{outcome.round_index + 1} "
          f"({outcome.adapter}/{outcome.model}/{outcome.effort}) repaired its "
          "own finding and no further round confirmed it. Every task keeps the "
          "status its own focused gate proved; only independent review "
          "confirmation is missing.")


def _auto_fix_journal_self_fixed(
        cfg: Config, state: auto_fix.AutoFixState,
        now: Callable[[], datetime], *, gate_evidence: str = "") -> None:
    """Record the settled outcome once on every implicated task's r file."""
    outcome = state.self_fixed_unreviewed
    assert outcome is not None
    ledger = {item.fingerprint: item for item in state.findings}
    by_task: dict[str, list[str]] = {}
    for fingerprint in outcome.finding_fingerprints:
        finding = ledger[fingerprint]
        if finding.task_id is None:
            continue
        by_task.setdefault(finding.task_id, []).append(
            f"- {fingerprint} {finding.path}: {finding.summary}")
    plan = Plan.parse(cfg.tasks_dir)
    identity = f"{outcome.adapter}/{outcome.model}/{outcome.effort}"
    for task_id, findings in by_task.items():
        task = plan.get(task_id)
        if task is None:
            continue
        append_entry(
            task.journal_path, by="scheduler",
            event="auto_fix_self_fixed_unreviewed",
            summary=(f"Review round {outcome.round_index + 1} of "
                     f"{outcome.rounds_used} ({identity}) repaired this task's "
                     "own finding and no further configured round confirmed it"),
            detail=("The configured per-plan review sequence ended on that "
                    "repair, so the folder is SELF-FIXED, UNREVIEWED: the task "
                    "keeps the status its own focused gate proved and nothing "
                    "was reopened, reverted, or marked BLOCKED. Only "
                    "independent review confirmation is missing; acceptance "
                    "remains the explicit human action.\n"
                    "Unconfirmed findings:\n" + "\n".join(findings)
                    + (f"\n{gate_evidence}" if gate_evidence else "")),
            time_str=now().isoformat(timespec="seconds"))


def _auto_fix_recover_dispositions(
        state: auto_fix.AutoFixState, plan: Plan) -> auto_fix.AutoFixState:
    """Rebuild valid post-checkpoint disposition evidence after a hard crash."""
    expected = state.current_finding_fingerprints
    dispositions = list(state.worker_dispositions)
    recorded = {(item.task_id, item.fingerprint) for item in dispositions}
    changed = False
    for task in plan.tasks:
        if task.status not in {"DONE", "BLOCKED"}:
            continue
        if all((task.id, fingerprint) in recorded for fingerprint in expected):
            continue
        try:
            entries = read_entries(task.journal_path)
        except AssentError:
            continue
        recovered = None
        for entry in reversed(entries):
            if entry.get("by") == "scheduler":
                continue
            try:
                recovered = auto_fix.parse_repair_dispositions(
                    entry.get("detail", ""), task_id=task.id,
                    task_status=task.status,
                    expected_fingerprints=expected)
            except AssentError:
                continue
            break
        if recovered is None:
            continue
        dispositions = [item for item in dispositions if item.task_id != task.id]
        dispositions.extend(recovered)
        recorded.update((item.task_id, item.fingerprint)
                        for item in recovered)
        changed = True
    if not changed:
        return state
    return auto_fix.with_worker_dispositions(state, tuple(dispositions))


def _run_auto_fix_repairs(
        cfg: Config, state: auto_fix.AutoFixState,
        rotation: _AdapterRotation, active: _ActiveTask, *,
        injected_reviewer: Adapter | None,
        sleep: Callable[[float], None],
        now: Callable[[], datetime],
        trusted_plan: Plan | None = None,
        trusted_contracts: dict[str, str] | None = None) -> int:
    """Walk the finite review-round sequence until a round passes or it runs out."""
    state_path = auto_fix.auto_fix_state_path(cfg)
    rounds = tuple(
        step for step in cfg.workflow_plan
        if isinstance(step, WorkflowPlanStep))
    try:
        reconciled, recovery_note = _reconcile_auto_fix_recovery_config(
            cfg, state)
        if reconciled != state:
            state = reconciled
            auto_fix.write_auto_fix_state(state_path, state)
        if recovery_note is not None:
            print(f"Auto-fix recovery: {recovery_note}.")
    except (AssentError, OSError) as e:
        print(f"Auto-fix recovery refused: {e}.")
        return 1
    if not state.repair_briefs:
        recovery_plan = Plan.parse(cfg.tasks_dir)
        state = _auto_fix_attach_repair_briefs(
            cfg, recovery_plan, state,
            blocker_evidence="Recovered from the durable finding ledger.",
            focused_evidence=(
                "Recovered focused or blocker evidence is embedded in each "
                "current finding."))
        auto_fix.write_auto_fix_state(state_path, state)
    # Reusable focused evidence belongs to one repair round: it is replaced when
    # the next round starts and is empty on a restart, so a RECHECK only ever
    # reuses a PASS its own round proved against the current state.
    round_passes = _FocusedGateLedger()
    while True:
        repair_step = None
        if (state.phase == "REPAIRING"
                and state.workflow_step_index
                and state.reviewer_step_index < state.workflow_step_index):
            previous_step = rounds[state.workflow_step_index - 1]
            if previous_step.writes:
                repair_step = previous_step
        if state.phase == "NEEDS_REPAIR":
            # A write-capable verdict role authorizes the ordinary task repair
            # its FAIL requested.  The separate blocked adjudication remains
            # read-only, but its durable decision must not consume the next
            # review round before the scheduler dispatches that repair.
            repair_authorized = (
                repair_step is not None
                or (state.workflow_step_index < len(rounds)
                    and state.reviewer_step_index < state.workflow_step_index
                    and rounds[state.reviewer_step_index].writes))
            while (not repair_authorized
                   and state.workflow_step_index < len(rounds)):
                step = rounds[state.workflow_step_index]
                if step.produces_verdict:
                    outcome = _run_auto_fix_review_once(
                        cfg, once=False, task_id=None,
                        injected_adapter=injected_reviewer, sleep=sleep, now=now,
                        trusted_plan=trusted_plan,
                        trusted_contracts=trusted_contracts,
                        gate_passes=round_passes)
                    if outcome.rounds_exhausted:
                        return _auto_fix_finish_rounds_exhausted(
                            cfg, outcome.state or state, now,
                            gate_passes=round_passes)
                    if outcome.code == 0:
                        return 0
                    if outcome.state is None or outcome.human_reason is not None:
                        return outcome.code
                    state = outcome.state
                    break
                state = auto_fix.with_workflow_step_index(
                    state, state.workflow_step_index + 1)
                auto_fix.write_auto_fix_state(state_path, state)
                if step.writes:
                    repair_authorized = True
                    repair_step = step
                    print(f"Auto-fix workflow step {state.workflow_step_index}: "
                          f"role {step.role!r} authorizes bounded repair with "
                          f"adapter candidates {' -> '.join(step.adapters)}.")
                    break
            if state.phase != "NEEDS_REPAIR":
                continue
            if not repair_authorized:
                if state.workflow_step_index >= len(rounds):
                    return _auto_fix_finish_rounds_exhausted(
                        cfg, state, now, gate_passes=round_passes)
                continue
        # Every reviewer decision that enters NEEDS_REPAIR is amended here,
        # not only the first one: a recheck may be what first exposes the
        # omitted path, and its addition must be durable before that decision
        # reopens the task or dispatches the next fixer round.  Re-entering
        # with nothing new left to apply is a validated no-op.
        if (state.phase in {"NEEDS_REPAIR", "AWAITING_REVIEW"}
                and state.approved_scope_additions):
            authoritative_plan = (
                _authoritative_status_plan(trusted_plan)
                if trusted_plan is not None else Plan.parse(cfg.tasks_dir))
            authoritative_contracts = (
                _authoritative_contracts(authoritative_plan, trusted_contracts)
                if trusted_contracts is not None
                else _task_contract_snapshots(authoritative_plan))
            state, trusted_plan, trusted_contracts = (
                _apply_reviewed_scope_amendments(
                    cfg, state, authoritative_plan, authoritative_contracts,
                    now))
        plan = Plan.parse(cfg.tasks_dir)
        recovered_state = _auto_fix_recover_dispositions(state, plan)
        if recovered_state != state:
            state = recovered_state
            auto_fix.write_auto_fix_state(state_path, state)
        try:
            record = auto_fix.validate_review_findings(
                auto_fix.current_review_record(state), plan)
        except AssentError as e:
            print(f"Auto-fix stopped for a human scope/plan decision: {e}")
            return 1
        implicated = list(dict.fromkeys(
            finding.task_id for finding in record.findings
            if finding.task_id is not None))
        if not implicated:
            print("Auto-fix stopped: no existing task owns the current findings.")
            return 1

        incomplete = [task for task in plan.tasks
                      if task.status not in ("DONE", "SKIP")]
        if state.phase == "AWAITING_REVIEW":
            if incomplete:
                shown = ", ".join(
                    f"{task.id}={task.status}" for task in incomplete)
                print("Auto-fix pending-review state is inconsistent with the "
                      f"task plan ({shown}); preserving it for human adjudication.")
                return 1
            outcome = _run_auto_fix_review_once(
                cfg, once=False, task_id=None,
                injected_adapter=injected_reviewer, sleep=sleep, now=now,
                trusted_plan=trusted_plan,
                trusted_contracts=trusted_contracts,
                gate_passes=round_passes)
            if outcome.code == 0:
                return 0
            if outcome.rounds_exhausted:
                return _auto_fix_finish_rounds_exhausted(
                    cfg, outcome.state or state, now,
                    gate_passes=round_passes)
            if outcome.state is None or outcome.human_reason is not None:
                return outcome.code
            state = outcome.state
            continue
        if state.phase == "REPAIRING" and not incomplete:
            # A task can reach its terminal checkpoint immediately before the
            # process dies.  The durable in-repair phase plus the DONE/SKIP
            # plan mechanically recovers that boundary without another fixer.
            state = auto_fix.with_repair_phase(state, "AWAITING_REVIEW")
            auto_fix.write_auto_fix_state(state_path, state)
            continue

        if state.phase == "NEEDS_REPAIR":
            reason = "Automatic repair of durable plan-review findings"
            if rework.rework_tasks_locked(cfg, implicated, reason) != 0:
                return 1
            state = auto_fix.with_repair_phase(state, "REPAIRING")
            auto_fix.write_auto_fix_state(state_path, state)
            plan = Plan.parse(cfg.tasks_dir)
        elif state.phase != "REPAIRING":
            print(f"Auto-fix stopped: unsupported repair phase {state.phase!r}.")
            return 1
        repair_tasks = [task for task in plan.tasks
                        if task.status in ("TODO", "WIP")]
        if not repair_tasks:
            print("Auto-fix repair has no runnable TODO/WIP task; preserving "
                  "the current statuses for human adjudication.")
            return 1

        # The configured review-round sequence makes this loop finite.  Its
        # fixer role selects the adapter and optional model/effort; omitted role
        # tiers keep the reopened task's own profile.
        state = auto_fix.with_worker_dispositions(state, ())
        auto_fix.write_auto_fix_state(state_path, state)

        failures: list[tuple[Task, str]] = []
        round_blockers: list[_AutoFixBlockerEvidence] = []
        round_passes = _FocusedGateLedger()
        round_dispositions: list[auto_fix.WorkerDisposition] = []
        for task in repair_tasks:
            try:
                profile = (_workflow_fixer_profile(cfg, task, repair_step)
                           if repair_step is not None
                           else _auto_fix_profile_for_task(cfg, task))
                task_rotation, sessions, errors = _fixer_rotation_and_sessions(
                    cfg, profile, rotation)
                if errors:
                    failures.append((task, "Fixer capability unavailable: "
                                     + "; ".join(errors)))
                    continue
                session = sessions[profile.adapter]
                append_entry(
                    task.journal_path, by="scheduler", event="auto_fix_attempt",
                    summary=("Bounded automatic repair session: "
                             + f"{profile.adapter}/{profile.model}/{profile.effort}"),
                    detail=("The durable finding ledger and repair brief were "
                            "persisted before this write-capable fixer session; "
                            "all edits and later gate evidence are preserved."),
                    agent=session.agent,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort,
                    time_str=now().isoformat(timespec="seconds"))
                session_state = _SessionState()
                active.task = task
                active.session = session_state
                failure = _process_task(
                    cfg, task, task_rotation, sleep, now, session_state,
                    resumed=task.status == "WIP",
                    session_overrides=sessions,
                    profile_model=profile.model,
                    auto_fix_context=_auto_fix_repair_context(task, state),
                    retry_limit=0, billing_is_failure=True,
                    blocker_evidence=round_blockers,
                    auto_fix_fingerprints=state.current_finding_fingerprints,
                    repair_dispositions=round_dispositions,
                    gate_passes=round_passes)
                active.task = None
                active.session = None
                if round_dispositions:
                    state = auto_fix.with_worker_dispositions(
                        state, tuple(round_dispositions))
                    auto_fix.write_auto_fix_state(state_path, state)
                if failure is not None:
                    failures.append((task, failure))
            except _AdapterProcessCreationError as e:
                active.task = None
                active.session = None
                print("Auto-fix fixer infrastructure failure before an AI child "
                      f"started; the same repair remains resumable: {e}")
                return 1
            except OSError as e:
                if active.session is not None and active.session.identity is not None:
                    _mark_interrupted_task(
                        task, active.session.identity,
                        "Auto-fix adapter infrastructure failure; progress kept",
                        now, detail=str(e))
                active.task = None
                active.session = None
                print("Auto-fix fixer infrastructure failure after the child-start "
                      "boundary could not be excluded; progress and the durable "
                      f"repair state were preserved: {e}")
                return 1

        if failures:
            if round_blockers:
                outcome = _run_auto_fix_review_once(
                    cfg, once=False, task_id=None,
                    injected_adapter=injected_reviewer, sleep=sleep, now=now,
                    blockers=tuple(round_blockers))
                if outcome.rounds_exhausted:
                    return _auto_fix_finish_rounds_exhausted(
                        cfg, outcome.state or state, now,
                        gate_passes=round_passes)
                if outcome.state is None or outcome.human_reason is not None:
                    return outcome.code
                state = outcome.state
            else:
                state = _auto_fix_failure_state(cfg, state, failures)
            continue
        state = auto_fix.with_repair_phase(state, "AWAITING_REVIEW")
        auto_fix.write_auto_fix_state(state_path, state)


def _recover_or_ensure_clean(cfg: Config, now: Callable[[], datetime]) -> None:
    """Run-startup cleanliness gate, extended with a provably-attributable recovery path.

    A hard power loss or a forced kill never reaches the Ctrl+C / quota / infrastructure
    interrupt handlers, so it leaves the worktree dirty with the task status and the wip
    checkpoint unwritten.  A writable plan-review session records its exact plan scope before
    launch, so an interruption after its first write leaves a durable ownership boundary too.
    On the next run, changes inside that boundary -- or changes attributable to the task
    ``next_task()`` would resume, a single DONE task the scheduler never checkpointed, or the
    single task a durable repair round implicates -- are gathered into a ``wip`` checkpoint and
    the run continues with no recovery AI session.  Every other dirty state (a change outside
    scope, ambiguous ownership, or no provable owner at all) keeps the fail-closed behaviour:
    ``ensure_clean`` raises and the caller refuses to run rather than guessing attribution.
    """
    if _try_recover_attributable_worktree(cfg, now):
        return
    gitops.ensure_clean(cfg.root, cfg.git_excludes)


@dataclass(frozen=True)
class _DirtOrigin:
    """What left a provably attributable dirty worktree behind, in the recovery's own wording.

    The checkpoint that gathers the dirt is the same one either way; the origin decides what
    the checkpoint subject, the terminal line and the journal entry say about where the dirt
    came from, so a human reading either afterwards can tell an unclean process exit from an
    interrupted review round without digging, and whether the owning task is reopened.
    """

    description: str
    checkpoint_reason: str
    journal_detail: str
    # Whether recovery may itself put the owning task back to WIP.  A crash before the
    # scheduler accepted anything leaves an untrusted status the next run must resume, but a
    # task a review round was repairing already passed its own focused gate and reached its
    # terminal checkpoint: only the durable repair loop's own rework may reopen that one, so
    # recovery gathers the edits and leaves every status exactly as its gate proved it.
    reopens_task: bool


_UNCLEAN_EXIT_DIRT = _DirtOrigin(
    "an unclean process exit",
    "recovered dirty worktree from an unclean exit, scope-verified",
    "run startup detected a dirty worktree from an unclean process "
    "exit and scope-verified it against the resumable candidate",
    reopens_task=True)

_INTERRUPTED_REVIEW_DIRT = _DirtOrigin(
    "an interrupted review round",
    "recovered dirty worktree from an interrupted review round, scope-verified",
    "run startup detected a dirty worktree left by a merged reviewer-fixer round "
    "that was interrupted after it wrote and before its verdict was recorded, and "
    "scope-verified it against the task the durable current findings implicate",
    reopens_task=False)


def _try_recover_attributable_worktree(cfg: Config,
                                       now: Callable[[], datetime]) -> bool:
    """Return True only when the worktree was dirty and every change was provably attributable
    to one task, having just committed that progress into a wip checkpoint.

    A writable plan reviewer has first claim through its pre-launch scope boundary.  Otherwise
    three task owners can be proven, in order: the task ``next_task()`` would resume; a single
    DONE task the scheduler never checkpointed; and the task a durable in-flight repair round
    implicates.  A clean worktree or dirt without a proven boundary returns False, leaving the
    caller's fail-closed ``ensure_clean`` to decide.  Every path still passes through
    ``changes_outside_scope`` against the scope captured before the AI session started.
    """
    if gitops.working_tree_status(cfg.root, cfg.git_excludes).is_clean:
        auto_fix.clear_auto_fix_review_session(cfg)
        return False
    review_scope = auto_fix.read_auto_fix_review_session(cfg)
    if review_scope is not None:
        outside = gitops.changes_outside_scope(
            cfg.root, list(review_scope), excludes=cfg.git_excludes)
        if outside:
            print("Interrupted plan-review output extends outside its recorded "
                  "scope; the worktree is retained for explicit human recovery "
                  f"({', '.join(outside[:8])}).")
            return False
        if not gitops.commit_if_dirty(
                cfg.root,
                f"wip({cfg.tasks_name}): recovered interrupted plan-review "
                "output, scope-verified",
                cfg.git_excludes):
            raise AssentError(
                "Interrupted plan-review output could not be checkpointed")
        auto_fix.clear_auto_fix_review_session(cfg)
        print("Recovered interrupted plan-review output; scope-verified "
              "against the recorded plan-review boundary and kept in a WIP "
              "checkpoint.")
        return True
    try:
        plan = Plan.parse(cfg.tasks_dir)
    except AssentError:
        return False
    try:
        cursor = read_workflow_state(cfg.tasks_dir)
    except AssentError:
        return False
    if cursor is not None and cursor.unit == "plan":
        if gitops.changes_outside_scope(
                cfg.root, _plan_scope(plan), excludes=cfg.git_excludes):
            return False
        gitops.commit_if_dirty(
            cfg.root,
            f"wip({cfg.tasks_name}): recovered plan workflow progress",
            cfg.git_excludes)
        print("Recovered dirty plan-workflow progress; scope-verified against "
              "the plan scope union and kept in a wip checkpoint.")
        return True
    origin = _UNCLEAN_EXIT_DIRT
    owner = _resumable_dirt_owner(cfg, plan) or _uncheckpointed_done_dirt_owner(cfg, plan)
    if owner is None:
        owner = _interrupted_review_round_dirt_owner(cfg, plan)
        origin = _INTERRUPTED_REVIEW_DIRT
    if owner is None:
        return False

    _mark_recovered_task(owner, now, origin)
    subject = _checkpoint_subject(cfg, "wip", owner, origin.checkpoint_reason)
    gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes)
    print(f"Recovered a dirty worktree from {origin.description}; scope-verified "
          f"against {owner.id}, progress kept in a wip checkpoint.")
    return True


def _resumable_dirt_owner(cfg: Config, plan: Plan) -> Task | None:
    """The resumable candidate task, when its scope contains every uncommitted change."""
    selection = plan.next_task()
    if selection is None:
        return None
    candidate, _ = selection
    if gitops.changes_outside_scope(
            cfg.root, candidate.scope, excludes=_task_excludes(cfg, candidate)):
        return None
    return candidate


def _uncheckpointed_done_dirt_owner(cfg: Config, plan: Plan) -> Task | None:
    """The one DONE task that provably owns the dirt after a crash between the execution AI's
    DONE mark and the scheduler's ``auto(...)`` checkpoint; None when ownership is not provable.

    That crash leaves the produced work uncommitted while ``next_task()`` has already moved on
    to the following TODO task, so the resumable-candidate path above must never be allowed to
    claim it.  Eligibility needs both halves of the evidence: no ``auto(<folder>/<task>)``
    checkpoint in this branch's history, and a scope containing every uncommitted path.  A
    missing checkpoint alone proves nothing -- a DONE task whose changes all landed in an
    earlier ``wip`` checkpoint legitimately has none -- so a second plausible owner, an existing
    terminal checkpoint, out-of-scope dirt, or unreadable history all fail closed.
    """
    try:
        history = gitops.commit_history(cfg.root)
    except AssentError:
        return None
    subjects = {subject for _commit, _parents, subject in history}

    owners = [
        task for task in plan.tasks
        if task.status == "DONE"
        and not any(s.startswith(f"auto({cfg.tasks_name}/{task.id}): ")
                    for s in subjects)
        and not gitops.changes_outside_scope(
            cfg.root, task.scope, excludes=_task_excludes(cfg, task))
    ]
    return owners[0] if len(owners) == 1 else None


def _interrupted_review_round_dirt_owner(cfg: Config, plan: Plan) -> Task | None:
    """The task an in-flight review round implicates, when its scope contains every uncommitted
    change; None when that ownership is not provable.

    A merged reviewer-fixer round may repair source itself, so interrupting one after its first
    write and before its verdict leaves dirt neither owner above can claim: the round writes no
    durable verdict (its position must not advance), and the task it was repairing already
    completed its ordinary closeout, so it has its terminal ``auto(...)`` checkpoint.  The
    folder's durable ``_auto_fix.toml`` supplies the missing evidence -- a ``REPAIRING`` or
    ``AWAITING_REVIEW`` phase is a round that was in flight, and its current findings name the
    tasks that round was allowed to write in.  Everything else fails closed: no durable state,
    a settled or not-in-flight phase, an unreadable record, dirt outside the implicated scope,
    and two implicated tasks that could each own it all return None.
    """
    try:
        state = _auto_fix_existing_state(cfg)
    except AssentError:
        return None
    if (state is None or state.phase not in ("REPAIRING", "AWAITING_REVIEW")
            or state.self_fixed_unreviewed is not None
            or state.unresolved_review is not None):
        return None
    ledger = {finding.fingerprint: finding for finding in state.findings}
    implicated = {
        ledger[fingerprint].task_id
        for fingerprint in state.current_finding_fingerprints
        if fingerprint in ledger and ledger[fingerprint].task_id is not None}
    owners = [
        task for task in plan.tasks
        if task.id in implicated
        and not gitops.changes_outside_scope(
            cfg.root, task.scope, excludes=_task_excludes(cfg, task))
    ]
    return owners[0] if len(owners) == 1 else None


def _mark_recovered_task(task: Task, now: Callable[[], datetime],
                         origin: _DirtOrigin) -> None:
    """Persist a recovered candidate as WIP and journal the scope-verified recovery in the
    wording of the ``origin`` that proved it.

    Unlike ``_mark_interrupted_task`` there is no AI session at this point, so no
    ``agent`` / ``requested_model`` / ``requested_effort`` identity exists -- a hard crash and
    an interrupted review round alike leave those genuinely unknown, and no fake session
    identity is fabricated to fill them
    (``append_entry`` accepts a ``scheduler`` entry with those fields omitted).  BLOCKED is
    preserved as a legal terminal state (though ``next_task()`` never yields a BLOCKED
    candidate); a secondary write error is only warned about so it never masks the recovery.
    """
    summary = (f"Recovered a dirty worktree from {origin.description}; "
               f"scope-verified against {task.id}, progress kept")
    if origin.reopens_task:
        try:
            fresh = parse_task_file(task.path)
            if fresh.status != "BLOCKED":
                set_status(task.path, "WIP")
        except Exception as e:  # recovery must not mask itself with a secondary status-write error
            print(f"Writing back the recovered task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="interrupt",
            summary=summary, detail=origin.journal_detail,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the recovery journal failed: {e} (working tree left as is, nothing discarded)")


def _mark_interrupted_task(task: Task, session: SessionIdentity, summary: str,
                           now: Callable[[], datetime], *, detail: str) -> None:
    """On abort, persist the current task as WIP and write a machine journal entry; a
    secondary error is only warned about.

    BLOCKED is a legal terminal state the execution AI can produce and must not be overwritten
    by the interrupt handling. DONE is not yet a trusted terminal state until the scheduler's
    acceptance passes, so it is still reverted to WIP, letting the next run resume it and pass
    the gates again.
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # abort cleanup must not mask the original exit code with a secondary error
        print(f"Writing back the interrupted task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="interrupt",
            summary=summary, detail=detail,
            agent=session.agent, requested_model=session.requested_model,
            requested_effort=session.requested_effort,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the interrupt journal failed: {e} (working tree left as is, nothing discarded)")


def _mark_billing_task(task: Task, session: SessionIdentity, detail: str,
                       now: Callable[[], datetime]) -> None:
    """On a billing abort, persist the task as WIP and write a distinct billing journal entry.

    The status is left unresolved (WIP, never BLOCKED) so the next run resumes it cleanly once
    the account is topped up; the work already produced stays in the tree.  The summary names
    the manual top-up requirement so it is unmistakable in the journal, separate from a normal
    acceptance failure.  A secondary error here is only warned about, never masking the abort.
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # abort cleanup must not mask the billing abort with a secondary error
        print(f"Writing back the billing task status failed: {e} (working tree left as is, nothing discarded)")

    try:
        append_entry(
            task.journal_path, by="scheduler", event="billing",
            summary=("Aborted: account balance/credit exhausted; this needs a manual "
                     "top-up, then rerun to resume (no retry consumed, progress kept)"),
            detail=detail,
            agent=session.agent, requested_model=session.requested_model,
            requested_effort=session.requested_effort,
            time_str=now().isoformat(timespec="seconds"))
    except Exception as e:  # status and journal are attempted independently; one failing does not block the other
        print(f"Writing the billing journal failed: {e} (working tree left as is, nothing discarded)")


def _preserve_interrupted_progress(cfg: Config, task: Task,
                                   session: SessionIdentity, *, event: str,
                                   summary: str, detail: str,
                                   checkpoint_reason: str,
                                   now: Callable[[], datetime]) -> None:
    """Record a resumable adapter interruption, checkpoint its dirty progress, and refresh.

    Quota exhaustion and the provider-neutral checkpoint-resume control have different
    continuation decisions, but both must preserve the same token-burned work before that
    decision.  The task is persisted as WIP first so a result that arrived after the execution
    AI wrote DONE cannot skip the next run's resume path; an explicit BLOCKED result remains
    untouched.  The caller owns the subsequent wait/rotation choice and sets the resume prompt.
    """
    try:
        fresh = parse_task_file(task.path)
        if fresh.status != "BLOCKED":
            set_status(task.path, "WIP")
    except Exception as e:  # status persistence must not discard progress or hide the interruption
        print(f"Writing back the resumable task status failed: {e} (working tree left as is, nothing discarded)")

    append_entry(task.journal_path, by="scheduler", event=event,
                 summary=summary, detail=detail,
                 agent=session.agent,
                 requested_model=session.requested_model,
                 requested_effort=session.requested_effort,
                 time_str=now().isoformat(timespec="seconds"))
    if gitops.commit_if_dirty(
            cfg.root, _checkpoint_subject(cfg, "wip", task, checkpoint_reason),
            cfg.git_excludes):
        print("  wip checkpoint created.")
    try_write_report(cfg)


def _commit_terminal_checkpoint(cfg: Config, task: Task, *, resumed: bool,
                                session_state: _SessionState) -> bool:
    """Create the one terminal auto checkpoint after a task passes every acceptance gate.

    Ordinary success keeps its existing dirty-tree behavior.  A task that resumed from WIP is
    different: its content may already be present in the preceding WIP commit, so a clean tree
    still needs one empty, namespaced auto marker proving terminal ownership.  The caller invokes
    this once, only after ``_evaluate`` returns ``done``.
    """
    subject = _checkpoint_subject(cfg, "auto", task, _short(task.title) or "done")
    pre_attempt_head = gitops.head_ref(cfg.root)
    if pre_attempt_head is not None:
        # This in-memory witness is intentionally armed at the closeout boundary, not at task
        # start.  It lets Ctrl+C recover a commit that succeeded just before the return path.
        session_state.terminal_checkpoint_attempt = (pre_attempt_head, subject)
    else:
        session_state.terminal_checkpoint_attempt = None
    if gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
        return True
    if resumed:
        gitops.commit_empty(cfg.root, subject)
        return True
    # No terminal operation was needed for a non-resumed clean task.  Do not leave a witness
    # armed while the post-closeout report is being refreshed.
    session_state.terminal_checkpoint_attempt = None
    return False


def _terminal_checkpoint_matches_attempt(
        cfg: Config, attempt: tuple[str, str] | None) -> bool:
    """Prove that an interrupted closeout created its exact auto commit.

    The attempt is armed only immediately before the scheduler's terminal commit, after
    ``_evaluate`` has passed.  Requiring first-parent history to contain the recorded subject
    with the recorded pre-attempt HEAD as its only parent rejects matching commits made by the
    execution AI, a retry, or any earlier phase of the task.
    """
    if attempt is None:
        return False
    base_ref, expected = attempt
    try:
        history = gitops.commit_history(cfg.root)
    except AssentError:
        return False
    return any(subject == expected and parents == (base_ref,)
               for _commit, parents, subject in history)


def _handle_main_tree_escape(cfg: Config, task: Task | None, baseline: set[str],
                             now: Callable[[], datetime], *,
                             plan: Plan | None = None) -> str | None:
    """Detect and, where possible, port back paths a just-finished session wrote into the main
    tree (``cfg.source_root``) instead of its isolated worktree (``cfg.root``).

    ``baseline`` is the main tree's dirty-path snapshot taken immediately before the session
    started; the diff against a fresh snapshot is what the session wrote. No new dirt ->
    returns None and the caller proceeds exactly as before (byte-for-byte identical to a run
    without this check). Any new dirt always makes this attempt's evaluation fail -- a session
    that wrote outside its isolated worktree cannot be trusted to have produced attributable,
    verifiable output, regardless of what verify/status on the worktree side would otherwise
    have said:
    - every escaped path inside the task scope or plan scope union -> ported into the
      worktree and restored in the main tree (all-or-nothing), with a mechanical scheduler
      journal record for every participating task describing exactly what moved;
    - any escaped path outside scope -> unattributable, main tree left untouched entirely;
    - a proven in-scope path that still fails to port (e.g. the worktree copy already
      diverged) -> fail closed, both trees left untouched, needs a human to port manually.

    Known limitation (accepted): under parallel folder runs, scope attribution of concurrent
    main-tree dirt is heuristic; overlapping scope between parallel tasks can misattribute a
    path, but the fail-closed branch above guarantees it never silently corrupts the main
    tree's content.
    """
    if plan is None:
        assert task is not None
        scope = task.scope
        excludes = _task_excludes(cfg, task)
        scope_label = "this task's scope"
        journal_tasks = (task,)
    else:
        scope = _plan_scope(plan)
        excludes = cfg.git_excludes
        scope_label = "the plan's scope union"
        journal_tasks = tuple(item for item in plan.tasks if item.status != "SKIP")

    current = gitops.dirty_paths(cfg.source_root, excludes)
    escaped = sorted(current - baseline)
    if not escaped:
        return None

    outside = set(gitops.changes_outside_scope(
        cfg.source_root, scope, excludes=excludes))
    outside_escaped = sorted(outside & set(escaped))
    if outside_escaped:
        shown = ", ".join(outside_escaped[:5]) + (" ..." if len(outside_escaped) > 5 else "")
        return (f"session wrote outside the isolated worktree, outside {scope_label} "
                f"(main tree not touched): {shown}")

    ok, apply_reason = gitops.port_back_main_tree_escape(
        cfg.source_root, cfg.root, escaped)
    if not ok:
        return (f"session wrote outside the isolated worktree; automatic port-back failed "
                f"({apply_reason}); main tree and worktree left unchanged, port back manually")

    shown = ", ".join(escaped[:5]) + (" ..." if len(escaped) > 5 else "")
    time_str = now().isoformat(timespec="seconds")
    for journal_task in journal_tasks:
        append_entry(
            journal_task.journal_path, by="scheduler", event="main_tree_escape",
            summary=(f"Ported {len(escaped)} path(s) that escaped into the main tree "
                     "back into the isolated worktree"),
            detail=f"paths ported back and restored in the main tree: {', '.join(escaped)}",
            time_str=time_str)
    return f"session wrote outside the isolated worktree; changes ported back ({shown})"


def _repair_dispositions_from_journal(
        task: Task, journal_start: int,
        expected_fingerprints: tuple[str, ...]) -> tuple[auto_fix.WorkerDisposition, ...]:
    """Validate the final worker-authored closeout entry for one repair session."""
    entries = read_entries(task.journal_path)
    worker_entries = [
        item for item in entries[journal_start:]
        if item.get("by") != "scheduler"
    ]
    if not worker_entries:
        raise AssentError("Repair closeout has no worker-authored journal entry")
    fresh = parse_task_file(task.path)
    return auto_fix.parse_repair_dispositions(
        worker_entries[-1].get("detail", ""), task_id=task.id,
        task_status=fresh.status,
        expected_fingerprints=expected_fingerprints)


def _plan_scope(plan: Plan) -> list[str]:
    return list(dict.fromkeys(path for task in plan.tasks for path in task.scope))


def _plan_verify_commands(plan: Plan) -> list[str]:
    return list(dict.fromkeys(task.verify for task in plan.tasks))


def _plan_cumulative_diff(cfg: Config, base_ref: str) -> str:
    result = subprocess.run(
        ["git", "diff", "--find-renames", base_ref, "--"], cwd=str(cfg.root),
        capture_output=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AssentError(
            "Unable to render plan workflow diff: "
            + _bounded_adapter_diagnostic(result.stderr or result.stdout))
    return (result.stdout or "(no cumulative changes)").rstrip()


def _plan_worker_prompt(
        cfg: Config, plan: Plan, step: WorkflowPlanStep, state: WorkflowState,
        trusted_contracts: dict[str, str], failure_reason: str | None) -> str:
    contracts_text = "\n\n".join(
        f"--- {task.id}: {task.path} ---\n{trusted_contracts[task.id].rstrip()}"
        for task in plan.tasks)
    completed = ""
    if state.step_index > 0:
        focused = tuple(
            item for item in state.focused_evidence
            if not item.startswith(_PLAN_FOCUSED_SWEEP_PREFIX))
        completed = (
            "\nCumulative checkpoint diff and working changes:\n"
            f"{_plan_cumulative_diff(cfg, state.base_ref)}\n\n"
            "Focused evidence from completed plan steps:\n"
            + "\n".join(focused) + "\n"
            + _focused_sweep_prompt(state))
    prompt = _PLAN_WORKER_PROMPT.format(
        agents_md_path=_agents_md_path_for_prompt(cfg),
        instructions_path=contracts.instructions_path(),
        position=state.step_index + 1, total=len(cfg.workflow_plan),
        role=step.role, role_policy="\n\n".join(
            ability.prompt for ability in step.resolved_role.abilities),
        scope="\n".join(f"- {path}" for path in _plan_scope(plan)),
        verify_commands="\n".join(
            f"- {command}" for command in _plan_verify_commands(plan)),
        contracts=contracts_text,
        dependency_state=_auto_fix_dependency_state(plan),
        completed_context=completed)
    if state.started and failure_reason is None:
        prompt += _RESUME_SUFFIX
    if failure_reason:
        prompt += _RETRY_SUFFIX.replace("{failure_reason}", failure_reason)
    return prompt


def _evaluate_plan_step(
        cfg: Config, plan: Plan, base_ref: str,
        trusted_contracts: dict[str, str]) -> tuple[bool, str, tuple[str, ...]]:
    for task in plan.tasks:
        try:
            current = task.path.read_text(encoding="utf-8")
        except OSError as error:
            return False, f"Task contract became unreadable: {error}", ()
        if current != trusted_contracts[task.id]:
            return False, f"Plan worker modified protected task contract {task.id}", ()
    outside = gitops.changes_outside_scope(
        cfg.root, _plan_scope(plan), since_ref=base_ref,
        excludes=cfg.git_excludes)
    if outside:
        shown = ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else "")
        return False, f"Changes outside the plan scope union appeared: {shown}", ()
    evidence: list[str] = []
    for command in _plan_verify_commands(plan):
        rc = _run_verify(cfg, command)
        evidence.append(f"{'PASS' if rc == 0 else 'FAIL'}: exit {rc}: {command}")
        if rc != 0:
            return False, ("Plan workflow gate exit code is non-zero "
                           f"(={rc}): {command}"), tuple(evidence)
    try:
        contract = _shared_paths_contract(cfg)
    except AssentError as error:
        return False, f"Shared-path contract could not be classified: {error}", tuple(evidence)
    refusal = shared_paths.closeout_refusal(contract)
    if refusal:
        return False, refusal[:1].upper() + refusal[1:], tuple(evidence)
    return True, "", tuple(evidence)


def _finish_plan_unit(
        cfg: Config, plan: Plan, step: WorkflowPlanStep | WorkflowActionStep,
        now: Callable[[], datetime]) -> None:
    step_name = step.role if isinstance(step, WorkflowPlanStep) else step.action
    for task in plan.tasks:
        if task.status != "SKIP":
            set_status(task.path, "DONE")
            workflow_source = (
                "task workflow = []" if task.workflow == ()
                else "[workflow].task = []")
            append_entry(
                task.journal_path, by="scheduler", event="done",
                summary=(f"Plan workflow step {step_name!r} completed "
                         f"the plan accountability unit for {task.id}"),
                detail=(
                    f"{workflow_source}; completed by plan step {step_name!r}; "
                    + ("focused plan gate passed."
                       if isinstance(step, WorkflowPlanStep)
                       else "scheduler-owned focused_sweep action passed.")),
                time_str=now().isoformat(timespec="seconds"))
    workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
    subject = f"auto({cfg.tasks_name}): workflow plan step {step_name}"
    if not gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
        gitops.commit_empty(cfg.root, subject)


def _block_plan_unit(
        cfg: Config, plan: Plan,
        step: WorkflowPlanStep | WorkflowActionStep, reason: str,
        now: Callable[[], datetime]) -> None:
    step_name = step.role if isinstance(step, WorkflowPlanStep) else step.action
    for task in plan.tasks:
        if task.status != "SKIP":
            set_status(task.path, "BLOCKED")
            append_entry(
                task.journal_path, by="scheduler", event="blocked",
                summary=f"Plan workflow step {step_name!r} exhausted retries",
                detail=reason,
                time_str=now().isoformat(timespec="seconds"))
    workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
    subject = f"auto({cfg.tasks_name}): BLOCKED (plan step {step_name})"
    if not gitops.commit_if_dirty(cfg.root, subject, cfg.git_excludes):
        gitops.commit_empty(cfg.root, subject)


def _process_plan_workflow(
        cfg: Config, plan: Plan, rotation: _AdapterRotation,
        sleep: Callable[[float], None], now: Callable[[], datetime],
        trusted_contracts: dict[str, str]) -> int:
    """Run a `[workflow].task = []` folder as one plan-owned unit."""
    if not cfg.workflow_plan:
        print("[workflow].task = [] has no [workflow].plan step to execute")
        return 1
    if all(task.status in ("DONE", "SKIP") for task in plan.tasks):
        _print_summary(plan)
        try_write_report(cfg)
        return 0
    state = read_workflow_state(cfg.tasks_dir)
    if state is None:
        state = WorkflowState(
            "plan", "", 0, False, gitops.head_ref(cfg.root) or "HEAD")
        write_workflow_state(cfg.tasks_dir, state)
    if state.unit != "plan" or state.step_index >= len(cfg.workflow_plan):
        raise AssentError("Workflow cursor does not match the configured plan unit")
    attempts = 0
    failure_reason: str | None = None
    step_rotation: _AdapterRotation | None = None
    step_rotation_index: int | None = None
    failed_step_adapters: set[str] = set()
    while True:
        refreshed = _refresh_focused_sweep_evidence(cfg, plan, state)
        if refreshed != state:
            state = refreshed
            write_workflow_state(cfg.tasks_dir, state)
        step = cfg.workflow_plan[state.step_index]
        if isinstance(step, WorkflowActionStep):
            print(f"\nPlan workflow step {state.step_index + 1}/"
                  f"{len(cfg.workflow_plan)}: {step.action}")
            gitops.commit_if_dirty(
                cfg.root,
                f"wip({cfg.tasks_name}): plan workflow before {step.action}",
                cfg.git_excludes)
            state, record, _reused = _run_focused_sweep_action(
                cfg, plan, state)
            final = state.step_index == len(cfg.workflow_plan) - 1
            later_role = (not final and isinstance(
                cfg.workflow_plan[state.step_index + 1], WorkflowPlanStep))
            if record.status == "PASSED":
                _finish_plan_unit(cfg, plan, step, now)
                _print_summary(Plan.parse(cfg.tasks_dir))
                return 0
            if record.status != "PASSED" and not later_role:
                reason = (f"focused_sweep action evidence is {record.status} "
                          f"(exit {record.exit_code})")
                _block_plan_unit(cfg, plan, step, reason, now)
                print("Plan workflow: REVIEW UNRESOLVED, HUMAN DECISION; "
                      "the configured steps were exhausted while "
                      "focused_sweep was still failing. Evidence and edits "
                      "were preserved.")
                _print_summary(Plan.parse(cfg.tasks_dir))
                return 0
            state = replace(
                state, step_index=state.step_index + 1, started=False)
            write_workflow_state(cfg.tasks_dir, state)
            attempts = 0
            failure_reason = None
            continue

        if step_rotation_index != state.step_index:
            step_rotation = rotation.subset(step.adapters)
            step_rotation_index = state.step_index
            failed_step_adapters.clear()
        assert step_rotation is not None
        adapter = step_rotation.adapter
        adapter_name = step_rotation.name
        session = _plan_step_session(cfg, adapter, plan, step, adapter_name)
        prompt = _plan_worker_prompt(
            cfg, plan, step, state, trusted_contracts, failure_reason)
        print(f"\nPlan workflow step {state.step_index + 1}/{len(cfg.workflow_plan)}: "
              f"{step.role}")
        state = replace(state, started=True)
        write_workflow_state(cfg.tasks_dir, state)
        main_tree_baseline = (
            gitops.dirty_paths(cfg.source_root, cfg.git_excludes)
            if cfg.source_root is not None else None)
        result = _invoke_adapter(
            cfg, adapter, adapter_name, prompt, session.requested_model,
            session.requested_effort, cfg.root, context_kind="plan",
            context_id=f"workflow.plan[{state.step_index}]")
        escape_reason = (
            _handle_main_tree_escape(
                cfg, None, main_tree_baseline, now, plan=plan)
            if main_tree_baseline is not None else None)
        if escape_reason is not None:
            reason = escape_reason
            passed, evidence = False, ()
        elif result.checkpoint_resume and not result.quota_exhausted:
            gitops.commit_if_dirty(
                cfg.root, f"wip({cfg.tasks_name}): plan workflow resume",
                cfg.git_excludes)
            failure_reason = None
            continue
        elif result.quota_exhausted:
            gitops.commit_if_dirty(
                cfg.root, f"wip({cfg.tasks_name}): plan workflow quota interrupt",
                cfg.git_excludes)
            if len(step_rotation.names) == 1:
                _wait_for_quota(cfg, result.reset_at, sleep, now)
            elif step_rotation.advance_after_quota(failed_step_adapters):
                _wait_for_rotation(cfg, sleep)
            failure_reason = None
            continue
        elif result.failure_kind == "authentication":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            gitops.commit_if_dirty(
                cfg.root,
                f"wip({cfg.tasks_name}): plan workflow authentication failover",
                cfg.git_excludes)
            action = _authentication_failover_action(
                step_rotation, failed_step_adapters, reason)
            if action == "switch":
                print(f"Plan workflow authentication failure: {reason}; "
                      f"switching {adapter_name} -> {step_rotation.name} "
                      "without consuming a retry.")
            else:
                print(f"Plan workflow authentication failure: {reason}; waiting "
                      f"only for recoverable adapter {step_rotation.name}.")
                _wait_for_rotation(cfg, sleep)
            failure_reason = None
            continue
        elif _adapter_availability_failed(result):
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            switched = step_rotation.advance_after_failure(
                failed_step_adapters)
            gitops.commit_if_dirty(
                cfg.root, f"wip({cfg.tasks_name}): plan workflow adapter failover",
                cfg.git_excludes)
            if switched:
                print(f"Plan workflow adapter failure: {reason}; switching "
                      f"{adapter_name} -> {step_rotation.name} without consuming "
                      "a retry.")
            else:
                print(f"Plan workflow adapters unavailable: {reason}; waiting "
                      f"{cfg.rotation_poll_minutes} minute(s) before restarting "
                      f"with {step_rotation.name}.")
                _wait_for_rotation(cfg, sleep)
            failure_reason = None
            continue
        elif result.exit_code != 0 or result.stalled:
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            passed, evidence = False, ()
        else:
            passed, reason, evidence = _evaluate_plan_step(
                cfg, plan, state.base_ref, trusted_contracts)
            refreshed = _refresh_focused_sweep_evidence(cfg, plan, state)
            if refreshed != state:
                state = refreshed
                write_workflow_state(cfg.tasks_dir, state)
            if passed and state.step_index == len(cfg.workflow_plan) - 1:
                focused_sweep_refusal = _focused_sweep_completion_refusal(
                    cfg, plan, state)
                if focused_sweep_refusal is not None:
                    passed, reason = False, focused_sweep_refusal
        if passed:
            combined = state.focused_evidence + evidence
            if state.step_index == len(cfg.workflow_plan) - 1:
                _finish_plan_unit(cfg, plan, step, now)
                final = Plan.parse(cfg.tasks_dir)
                _print_summary(final)
                try_write_report(cfg)
                return 0
            state = replace(
                state, step_index=state.step_index + 1, started=False,
                focused_evidence=combined)
            write_workflow_state(cfg.tasks_dir, state)
            attempts = 0
            failure_reason = None
            continue
        print(f"  Plan workflow acceptance failed: {reason}")
        if attempts < cfg.retry_per_task:
            attempts += 1
            failure_reason = reason
            continue
        _block_plan_unit(cfg, plan, step, reason, now)
        print("Plan workflow: REVIEW UNRESOLVED, HUMAN DECISION; the "
              "configured steps were exhausted without a passing "
              "focused_sweep. Evidence and edits were preserved.")
        final = Plan.parse(cfg.tasks_dir)
        _print_summary(final)
        try_write_report(cfg)
        return 0


def _process_task(cfg: Config, task: Task, rotation: _AdapterRotation,
                  sleep: Callable[[float], None],
                  now: Callable[[], datetime], session_state: _SessionState,
                  resumed: bool = False, *,
                   session_override: SessionIdentity | None = None,
                   session_overrides: dict[str, SessionIdentity] | None = None,
                  profile_model: str | None = None,
                  auto_fix_context: str = "",
                  retry_limit: int | None = None,
                  billing_is_failure: bool = False,
                  blocker_evidence: list[_AutoFixBlockerEvidence] | None = None,
                  auto_fix_fingerprints: tuple[str, ...] = (),
                  repair_dispositions: list[auto_fix.WorkerDisposition] | None = None,
                  gate_passes: _FocusedGateLedger | None = None,
                  usage_context_kind: str = "task",
                  usage_context_id: str | None = None,
                  usage_folders: tuple[str, ...] | None = None,
                  ) -> str | None:
    """Run a single task's full lifecycle; internally handles quota/control resumption and
    retries, and by the end the task is DONE/BLOCKED.

    `task` is the trusted version parsed at task-selection time (= the previous checkpoint):
    scope/verify and all fields are taken from it, and the only legal change the execution AI
    may make to the on-disk version is the status line (compared in _evaluate).
    """
    print(f"\nTask {task.id}: {task.title}")

    workflow = _effective_task_workflow(cfg, task) or ()
    workflow_state: WorkflowState | None = None
    if workflow:
        workflow_state = read_workflow_state(cfg.tasks_dir)
        if workflow_state is None:
            workflow_state = WorkflowState(
                "task", task.id, 0, False, gitops.head_ref(cfg.root) or "HEAD")
            write_workflow_state(cfg.tasks_dir, workflow_state)
        if (workflow_state.unit != "task" or workflow_state.task_id != task.id
                or workflow_state.step_index >= len(workflow)):
            raise AssentError(
                "Workflow cursor does not match the selected task workflow")
        if _task_scope_transactions(workflow_state):
            task = _recover_task_scope_amendments(cfg, workflow_state, now)
        resumed = workflow_state.started
    trusted_task_text = task.path.read_text(encoding="utf-8")
    if resumed:
        print("  (interrupted workflow step detected; resuming with a continue prompt)")

    # The HEAD at this task's start: the scope check must cover all changes since the start
    # (including wip checkpoints).
    start_ref = gitops.head_ref(cfg.root)

    attempts_used = 0
    failure_reason: str | None = None
    attempted_failures: list[tuple[str, str]] = []
    step_rotation: _AdapterRotation | None = None
    step_rotation_index: int | None = None
    failed_step_adapters: set[str] = set()
    # Repair disposition validation needs a precise journal boundary. Ordinary
    # sessions retain the historical behavior in which a malformed pre-existing
    # journal does not prevent the worker prompt or task execution from running.
    journal_start = (len(read_entries(task.journal_path))
                     if auto_fix_fingerprints else 0)
    while True:
        if workflow_state is not None:
            refreshed = _refresh_task_action_evidence(cfg, task, workflow_state)
            if refreshed != workflow_state:
                workflow_state = refreshed
                write_workflow_state(cfg.tasks_dir, workflow_state)
        workflow_step = (
            workflow[workflow_state.step_index]
            if workflow_state is not None else None)
        if isinstance(workflow_step, WorkflowActionStep):
            print(f"  Task workflow step {workflow_state.step_index + 1}/"
                  f"{len(workflow)}: {workflow_step.action}")
            gitops.commit_if_dirty(
                cfg.root,
                _checkpoint_subject(
                    cfg, "wip", task, f"before {workflow_step.action}"),
                cfg.git_excludes)
            assert workflow_step.action == "focused_test"
            workflow_state, record, _reused = _run_focused_test_action(
                cfg, task, workflow_state)
            final = workflow_state.step_index == len(workflow) - 1
            later_role = (not final and isinstance(
                workflow[workflow_state.step_index + 1], WorkflowTaskStep))
            if record.status == "PASSED":
                _fresh, reason = _inspect_task_safety(cfg, task, start_ref)
                if reason is None:
                    refreshed = _refresh_task_action_evidence(
                        cfg, task, workflow_state)
                    if refreshed != workflow_state:
                        workflow_state = refreshed
                        write_workflow_state(cfg.tasks_dir, workflow_state)
                    if any(isinstance(item, WorkflowActionStep)
                           and item.action == "focused_test"
                           for item in workflow):
                        reason = _focused_test_completion_refusal(workflow_state)
                    if reason is None:
                        _fresh, reason = _inspect_task_safety(
                            cfg, task, start_ref)
                if reason is None:
                    try:
                        contract = _shared_paths_contract(cfg)
                    except AssentError as error:
                        reason = f"Shared-path contract could not be classified: {error}"
                    else:
                        refusal = shared_paths.closeout_refusal(contract)
                        if refusal:
                            reason = refusal[:1].upper() + refusal[1:]
                if reason is None:
                    set_status(task.path, "DONE")
                    append_entry(
                        task.journal_path, by="scheduler", event="done",
                        summary=(f"Scheduler-owned {workflow_step.action} action completed "
                                 f"the task workflow for {task.id}"),
                        detail=(f"Command: {record.command}\n"
                                f"Exit code: {record.exit_code}\n"
                                f"Summary:\n{record.summary}"),
                        time_str=now().isoformat(timespec="seconds"))
                    workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
                    _commit_terminal_checkpoint(
                        cfg, task, resumed=True, session_state=session_state)
                    if gate_passes is not None:
                        gate_passes.record(cfg, task.verify)
                    print(f"  {workflow_step.action} and required task tests passed -> "
                          "creating checkpoint")
                    return None
                _block_task_action(cfg, task, record, reason, now)
                return reason
            if record.status != "PASSED" and not later_role:
                reason = (f"{workflow_step.action} action evidence is {record.status} "
                          f"(exit {record.exit_code})")
                _block_task_action(cfg, task, record, reason, now)
                return reason
            workflow_state = replace(
                workflow_state,
                step_index=workflow_state.step_index + 1,
                started=False)
            write_workflow_state(cfg.tasks_dir, workflow_state)
            attempts_used = 0
            failure_reason = None
            resumed = False
            continue

        assert workflow_step is None or isinstance(workflow_step, WorkflowTaskStep)
        if (workflow_step is not None and session_override is None
                and session_overrides is None):
            assert workflow_state is not None
            if step_rotation_index != workflow_state.step_index:
                step_rotation = rotation.subset(
                    workflow_step.adapters or rotation.names)
                step_rotation_index = workflow_state.step_index
                failed_step_adapters.clear()
            active_rotation = step_rotation
        else:
            active_rotation = rotation
            step_rotation = None
            step_rotation_index = None
            if session_overrides is None:
                failed_step_adapters.clear()
        assert active_rotation is not None
        adapter = active_rotation.adapter
        adapter_name = active_rotation.name
        session = ((session_overrides or {}).get(adapter_name)
                   or session_override or
                   (_workflow_task_session(
                       cfg, adapter, task, workflow_step, adapter_name)
                    if workflow_step is not None else
                    resolve_session(cfg, adapter, task, adapter_name)))
        session_state.identity = session
        prompt = _build_prompt(
            cfg, task, failure_reason, session, resumed,
            workflow_step=workflow_step,
            workflow_index=(workflow_state.step_index
                            if workflow_state is not None else 0),
            workflow_total=len(workflow),
            action_evidence=(
                (_focused_test_prompt(workflow_state)
                 + _task_blocker_prompt(workflow_state)
                 + _task_review_prompt(workflow_state))
                if workflow_state is not None else ""),
            focused_test_action_present=any(
                isinstance(item, WorkflowActionStep)
                and item.action == "focused_test" for item in workflow))
        if auto_fix_context:
            prompt += auto_fix_context
        print(_session_line(
            adapter_name, task, session,
            model=profile_model or (
                workflow_step.resolved_role.model
                if workflow_step is not None else None)))
        workflow_journal_start = (
            len(read_entries(task.journal_path))
            if workflow_step is not None else 0)
        if workflow_state is not None:
            workflow_state = replace(workflow_state, started=True)
            write_workflow_state(cfg.tasks_dir, workflow_state)
        main_tree_baseline = (gitops.dirty_paths(cfg.source_root, _task_excludes(cfg, task))
                              if cfg.source_root is not None else None)
        role_session_ref = gitops.head_ref(cfg.root)
        try:
            result = _invoke_adapter(
                cfg, adapter, adapter_name, prompt, session.requested_model,
                session.requested_effort, cfg.root,
                context_kind=usage_context_kind,
                context_id=usage_context_id or task.id,
                folders=usage_folders,
                structured=bool(
                    workflow_step is not None
                    and workflow_step.produces_verdict))
        except OSError as e:
            if _adapter_process_creation_failed(e):
                raise _AdapterProcessCreationError(str(e)) from e
            raise
        if (not result.quota_exhausted
                and result.failure_kind != "authentication"
                and not (_adapter_availability_failed(result)
                         and workflow_step is not None)):
            active_rotation.session_opened()
        escape_reason = (
            _handle_main_tree_escape(cfg, task, main_tree_baseline, now)
            if main_tree_baseline is not None else None)

        if escape_reason is not None:
            print(f"  {escape_reason}")
            outcome, reason = "fail", escape_reason
            focused_evidence = (
                "NOT RUN: the isolated-worktree escape safety gate failed first.")
        elif (result.checkpoint_resume and not result.quota_exhausted
              and not result.stalled and result.exit_code != 0):
            print("  Checkpoint-resume control received -> keep progress (wip checkpoint).")
            _preserve_interrupted_progress(
                cfg, task, session, event="checkpoint_resume",
                summary=("Checkpoint-resume requested; progress kept, immediately "
                         "resuming the same adapter command"),
                detail=("The adapter emitted the exact final control record "
                        '{"type":"assent.checkpoint_resume"}; no quota wait, '
                        "adapter rotation, or retry was used."),
                checkpoint_reason="checkpoint-resume control, progress kept", now=now)
            print("  Resuming the same adapter command immediately without waiting, "
                  "rotating, or consuming a retry.")
            resumed = True
            continue
        elif result.quota_exhausted:  # quota exhaustion does not count as a failure
            print("  Quota exhausted -> keep progress (wip checkpoint).")
            wait_kind: str | None = None
            if len(active_rotation.names) == 1:
                if result.reset_at is None:
                    quota_summary = (
                        "Quota exhausted; progress kept, waiting for quota poll "
                        f"(every {cfg.quota_poll_minutes} minutes) before resuming")
                    quota_action = (
                        "  Waiting for quota poll "
                        f"(every {cfg.quota_poll_minutes} minutes) before resuming...")
                else:
                    quota_summary = (
                        "Quota exhausted; progress kept, waiting for quota reset "
                        "before resuming")
                    quota_action = "  Waiting for quota reset before resuming..."
                wait_kind = "quota"
            else:
                cycle_exhausted = active_rotation.advance_after_quota(
                    failed_step_adapters if workflow_step is not None else None)
                if cycle_exhausted:
                    if active_rotation.auth_failed:
                        quota_summary = (
                            "Quota exhausted; progress kept, every recoverable "
                            "adapter is quota-exhausted; authentication-failed "
                            "candidates remain skipped while waiting for rotation "
                            f"poll before continuing with {active_rotation.name}")
                        quota_action = (
                            "  Every recoverable adapter is quota-exhausted; "
                            "authentication-failed candidates remain skipped. "
                            f"Waiting {cfg.rotation_poll_minutes} minute(s) before "
                            f"continuing with {active_rotation.name}.")
                    else:
                        quota_summary = (
                            "Quota exhausted; progress kept, every adapter in the "
                            "rotation is quota-exhausted; waiting for rotation poll "
                            f"before continuing with {active_rotation.name}")
                        quota_action = (
                            "  Every adapter in the rotation is quota-exhausted; "
                            f"waiting {cfg.rotation_poll_minutes} minute(s) before "
                            f"continuing with {active_rotation.name}.")
                    wait_kind = "rotation"
                else:
                    quota_summary = (
                        "Quota exhausted; progress kept, switching immediately "
                        f"to adapter {active_rotation.name}")
                    quota_action = (
                        f"  Switching adapter {adapter_name} -> {active_rotation.name} "
                        "immediately; resuming the same task without "
                        "consuming a retry.")
            _preserve_interrupted_progress(
                cfg, task, session, event="quota", summary=quota_summary,
                detail="Quota evidence preserved the current task progress for resumption.",
                checkpoint_reason="quota interrupt, progress kept", now=now)
            print(quota_action)
            if wait_kind == "quota":
                _wait_for_quota(cfg, result.reset_at, sleep, now)
            elif wait_kind == "rotation":
                _wait_for_rotation(cfg, sleep)
            resumed = True
            continue  # resume the same task, without counting a retry
        elif result.failure_kind == "billing":
            # A zero prepaid balance is an account-level condition, not a per-task one:
            # retrying cannot fix it and every following TODO task would fail identically.
            # Abort the whole run here (no retry consumed) so the abort handler keeps this
            # task's progress and leaves it unresolved for a clean resume after a top-up.
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            print(f"  Adapter failure: {reason}")
            if not billing_is_failure:
                raise _BillingAbort(reason)
            _append_adapter_failure_entry(
                task, session, result.exit_code, result.stalled, reason, now,
                failure_kind=result.failure_kind)
            outcome = "fail"
            focused_evidence = (
                "NOT RUN: the worker adapter failed before focused closeout.")
        elif result.failure_kind == "authentication":
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            action = active_rotation.advance_after_authentication(
                failed_step_adapters)
            if action == "switch":
                summary = ("Authentication failure; progress kept, switching "
                           f"{adapter_name} -> {active_rotation.name}")
                terminal = (f"  Switching adapter {adapter_name} -> "
                            f"{active_rotation.name} without consuming a retry.")
            elif action == "wait":
                summary = ("Authentication failure; progress kept, waiting only "
                           f"for recoverable adapter {active_rotation.name}")
                terminal = ("  Waiting only for recoverable adapter "
                            f"{active_rotation.name}; authentication failures "
                            "will not be retried.")
            else:
                summary = ("Authentication required for every declared adapter; "
                           "progress kept for an explicit rerun after login")
                terminal = "  Every declared adapter requires authentication."
            _preserve_interrupted_progress(
                cfg, task, session, event="authentication",
                summary=summary,
                detail=(f"{reason}\nDeclared adapter candidates: "
                        + ", ".join(active_rotation.names)),
                checkpoint_reason="authentication failure, progress kept", now=now)
            print(f"  Adapter failure: {reason}")
            print(terminal)
            if action == "required":
                raise _AuthenticationRequired(
                    _authentication_required_message(active_rotation, reason))
            if action == "wait":
                _wait_for_rotation(cfg, sleep)
            resumed = True
            continue
        elif (_adapter_availability_failed(result)
              and workflow_step is not None):
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            switched = active_rotation.advance_after_failure(
                failed_step_adapters)
            next_adapter = active_rotation.name
            _preserve_interrupted_progress(
                cfg, task, session, event="adapter_failover",
                summary=(f"Adapter failure; progress kept, switching "
                         f"{adapter_name} -> {next_adapter}"),
                detail=(f"{reason}\nDeclared adapter candidates: "
                        + ", ".join(active_rotation.names)),
                checkpoint_reason="adapter failure, progress kept", now=now)
            print(f"  Adapter failure: {reason}")
            if switched:
                print(f"  Switching adapter {adapter_name} -> {next_adapter} "
                      "without consuming a retry.")
            else:
                print("  Every declared adapter is unavailable; waiting "
                      f"{cfg.rotation_poll_minutes} minute(s) before restarting "
                      f"with {next_adapter}.")
                _wait_for_rotation(cfg, sleep)
            resumed = True
            continue
        elif result.exit_code != 0 or result.stalled:
            reason = _adapter_failure_reason(
                result.exit_code, result.stalled, result.output,
                result.failure_kind)
            _append_adapter_failure_entry(
                task, session, result.exit_code, result.stalled, reason, now,
                failure_kind=result.failure_kind)
            print(f"  Adapter failure: {reason}")

            # A failed subprocess cannot authorize either terminal result or run
            # verification.  Safety inspection still runs against its preserved changes so
            # that a later scheduler BLOCKED record retains every material failure reason.
            _fresh, safety_reason = _inspect_task_safety(
                cfg, task, start_ref)
            if safety_reason:
                reason = f"{reason}; safety inspection failed: {safety_reason}"
            outcome = "fail"
            focused_evidence = (
                "NOT RUN: the worker adapter or structural safety gate failed first.")
        elif (workflow_step is not None and workflow_state is not None
              and workflow_state.step_index < len(workflow) - 1):
            if workflow_step.produces_verdict:
                review_output = (result.structured_output
                                 if result.structured_output is not None
                                 else result.output)
                try:
                    if result.structured_output_error is not None:
                        raise AssentError(result.structured_output_error)
                    task, workflow_state = _evaluate_task_verdict_role(
                        cfg, task, workflow_step, workflow_state,
                        review_output, role_session_ref, now)
                    trusted_task_text = task.path.read_text(encoding="utf-8")
                except AssentError as error:
                    outcome = "fail"
                    reason = f"Task verdict closeout failed: {error}"
                    focused_evidence = (
                        "NOT RUN: task verdict validation failed before the "
                        "next focused_test action.")
                else:
                    outcome, reason, focused_evidence = _evaluate_task_workflow_step(
                        cfg, task, workflow_step, workflow_journal_start,
                        start_ref, role_session_ref)
            else:
                outcome, reason, focused_evidence = _evaluate_task_workflow_step(
                    cfg, task, workflow_step, workflow_journal_start,
                    start_ref, role_session_ref)
        else:
            focused_test_required = any(
                isinstance(item, WorkflowActionStep)
                and item.action == "focused_test" for item in workflow)
            if focused_test_required and workflow_state is not None:
                refreshed = _refresh_task_action_evidence(
                    cfg, task, workflow_state)
                if refreshed != workflow_state:
                    workflow_state = refreshed
                    write_workflow_state(cfg.tasks_dir, workflow_state)
            focused_test_record = (
                _focused_test_record(workflow_state)
                if focused_test_required and workflow_state is not None
                else None)
            outcome, reason, focused_evidence = _evaluate(
                cfg, task, start_ref,
                focused_test_record=focused_test_record,
                focused_test_required=focused_test_required)
            if workflow_step is not None and outcome in {"done", "self_blocked"}:
                worker_entries = [
                    item for item in read_entries(task.journal_path)[
                        workflow_journal_start:]
                    if item.get("by") != "scheduler"]
                if not worker_entries:
                    outcome = "fail"
                    reason = "Workflow session did not append its journal entry"
        if workflow_state is not None:
            refreshed = _refresh_task_action_evidence(
                cfg, task, workflow_state)
            if refreshed != workflow_state:
                workflow_state = refreshed
                write_workflow_state(cfg.tasks_dir, workflow_state)
        if (outcome == "self_blocked" and workflow_state is not None
                and workflow_step is not None):
            continuation = _task_blocked_continuation(
                workflow, workflow_state.step_index)
            if continuation is not None:
                worker_entries = [
                    item for item in read_entries(task.journal_path)[
                        workflow_journal_start:]
                    if item.get("by") != "scheduler"]
                worker_entry = worker_entries[-1]
                summary = str(
                    worker_entry.get("summary")
                    or "Task role self-marked BLOCKED")
                detail = str(worker_entry.get("detail") or focused_evidence)
                set_status(task.path, "WIP")
                workflow_state = _with_task_blocker_evidence(
                    workflow_state, workflow_step.role, summary, detail)
                workflow_state = replace(
                    workflow_state, step_index=continuation, started=False)
                write_workflow_state(cfg.tasks_dir, workflow_state)
                gitops.commit_if_dirty(
                    cfg.root, _checkpoint_subject(
                        cfg, "wip", task,
                        f"{workflow_step.role} BLOCKED; task repair pending"),
                    cfg.git_excludes)
                print("  Task role self-marked BLOCKED; advancing within "
                      "workflow.task to its configured verdict/repair role")
                attempts_used = 0
                failure_reason = None
                resumed = False
                continue
        if outcome == "step_done":
            assert workflow_state is not None and workflow_step is not None
            set_status(task.path, "WIP")
            completed_step = workflow_state.step_index
            next_step = completed_step + 1
            review_record = _task_review_record(workflow_state)
            if (workflow_step.produces_verdict
                    and review_record is not None
                    and review_record.verdict == "PASS"):
                while (next_step < len(workflow)
                       and isinstance(workflow[next_step], WorkflowTaskStep)):
                    next_step += 1
            workflow_state = replace(
                workflow_state, step_index=next_step,
                started=False,
                focused_evidence=(workflow_state.focused_evidence
                                  + (focused_evidence,)))
            write_workflow_state(cfg.tasks_dir, workflow_state)
            gitops.commit_if_dirty(
                cfg.root, _checkpoint_subject(
                    cfg, "wip", task,
                    f"workflow step {completed_step + 1} ({workflow_step.role})"),
                cfg.git_excludes)
            print(f"  Workflow step {completed_step + 1}/{len(workflow)} passed; "
                  "advancing to the next session")
            attempts_used = 0
            failure_reason = None
            resumed = False
            continue
        if outcome in {"done", "self_blocked"} and auto_fix_fingerprints:
            try:
                dispositions = _repair_dispositions_from_journal(
                    task, journal_start, auto_fix_fingerprints)
            except AssentError as e:
                outcome = "fail"
                reason = f"Repair disposition gate failed: {e}"
            else:
                if repair_dispositions is not None:
                    repair_dispositions.extend(dispositions)
        if outcome == "done":
            print("  Acceptance passed -> creating checkpoint")
            committed = _commit_terminal_checkpoint(
                cfg, task, resumed=resumed, session_state=session_state)
            # Report refresh is deliberately after the Git evidence.  Keep this marker before
            # entering it so Ctrl+C there cannot downgrade a task whose terminal ownership is
            # already recorded or create a second WIP checkpoint.
            session_state.terminal_checkpoint = committed
            if not committed:
                print("  (no new changes in the working tree; progress is already in a prior wip checkpoint)")
            elif resumed:
                print("  terminal auto checkpoint created; resumed progress was preserved")
            if gate_passes is not None:
                # Only this path reaches here with the scheduler's own focused
                # command having exited 0; the closeout-only probe and every
                # failed gate leave through the failure path below.
                gate_passes.record(cfg, task.verify)
            if workflow_state is not None:
                workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
            try_write_report(cfg)
            return None
        if outcome == "self_blocked":
            print("  Execution AI self-marked BLOCKED (legal output, handed to a human) -> creating checkpoint")
            if workflow_state is not None:
                workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
            gitops.commit_if_dirty(
                cfg.root, _checkpoint_subject(
                    cfg, "auto", task, "BLOCKED (execution AI self-marked)"),
                cfg.git_excludes)
            if blocker_evidence is not None:
                worker_entries = [
                    item for item in read_entries(task.journal_path)[journal_start:]
                    if item.get("by") != "scheduler"
                ]
                worker_reason = str(
                    worker_entries[-1].get("summary")
                    if worker_entries else "Execution AI self-marked BLOCKED")
                evidence = _AutoFixBlockerEvidence(
                    task, "worker_blocked", "Execution AI self-marked BLOCKED",
                    "NOT RUN: self-marked BLOCKED closeout legitimately skips the focused gate.",
                    worker_reason)
                blocker_evidence.append(evidence)
                append_entry(
                    task.journal_path, by="scheduler", event="auto_fix_blocker",
                    summary=evidence.reason,
                    detail=("Worker journal summary (verbatim):\n"
                            f"{worker_reason}\nFocused evidence:\n"
                            f"{evidence.focused_evidence}"),
                    agent=session.agent,
                    requested_model=session.requested_model,
                    requested_effort=session.requested_effort,
                    time_str=now().isoformat(timespec="seconds"))
            try_write_report(cfg)
            return "Execution AI self-marked BLOCKED"

        # outcome == "fail": no restore (output kept), retry with the reason; once retries are
        # exhausted the scheduler marks BLOCKED, and the not-yet-passing work is committed
        # together with the BLOCKED mark for a human to make the final call.
        print(f"  Acceptance failed: {reason}")
        attempted_failures.append((reason or "acceptance failed",
                                   focused_evidence))
        allowed_retries = (cfg.retry_per_task
                           if retry_limit is None else retry_limit)
        if attempts_used < allowed_retries:
            attempts_used += 1
            failure_reason = reason
            print(f"  Keeping existing work, retrying with the failure reason (attempt {attempts_used})...")
            continue
        print("  Retries exhausted -> scheduler marks BLOCKED (the not-yet-passing work is kept too)")
        task.path.write_text(trusted_task_text, encoding="utf-8", newline="")
        _mark_blocked(cfg, task, session, reason or "acceptance failed", now,
                      attempts=attempts_used)
        if workflow_state is not None:
            workflow_state_path(cfg.tasks_dir).unlink(missing_ok=True)
        if blocker_evidence is not None:
            combined_reason = " | ".join(dict.fromkeys(
                item[0] for item in attempted_failures))
            focused_items = list(dict.fromkeys(
                item[1] for item in attempted_failures))
            combined_focused = "\n".join(focused_items)
            trigger = ("focused_gate_failure"
                       if any(item.startswith("FAIL:")
                              for item in focused_items)
                       else "worker_blocked")
            evidence = _AutoFixBlockerEvidence(
                task, trigger, combined_reason, combined_focused)
            blocker_evidence.append(evidence)
            append_entry(
                task.journal_path, by="scheduler", event="auto_fix_blocker",
                summary=combined_reason,
                detail=f"Focused evidence:\n{combined_focused}",
                agent=session.agent,
                requested_model=session.requested_model,
                requested_effort=session.requested_effort,
                time_str=now().isoformat(timespec="seconds"))
        try_write_report(cfg)
        return reason or "acceptance failed"


def _inspect_task_safety(cfg: Config, task: Task,
                         start_ref: str | None = None) -> tuple[Task | None, str | None]:
    """Re-parse and inspect the non-model structural and scope safety floors.

    The checks deliberately collect both task-file tampering and out-of-scope changes when
    possible so an adapter failure does not hide independent safety evidence.
    """
    try:
        fresh = parse_task_file(task.path)
    except AssentError as e:
        return None, ("Re-parsing the task file failed (the execution AI may have broken "
                      f"it): {e}")

    issues: list[str] = []
    tampered = same_except_status(task, fresh)
    if tampered:
        issues.append(
            f"Task file fields other than status were modified: {', '.join(tampered)}"
            " (the execution AI may only change the status line)")

    outside = gitops.changes_outside_scope(
        cfg.root, task.scope, since_ref=start_ref,
        excludes=_task_excludes(cfg, task))
    if outside:
        shown = ", ".join(outside[:5]) + (" ..." if len(outside) > 5 else "")
        issues.append(f"Changes outside scope appeared: {shown}")
    return fresh, "; ".join(issues) if issues else None


def _evaluate_task_verdict_role(
        cfg: Config, task: Task, step: WorkflowTaskStep,
        state: WorkflowState, output: str, session_ref: str,
        now: Callable[[], datetime]) -> tuple[Task, WorkflowState]:
    """Validate one task verdict role and finish any exact scope transaction."""
    record = auto_fix.parse_review_output(output)
    focused = _focused_test_record(state)
    if (record.verdict == "PASS"
            and (_task_blocker_evidence(state) is None
                 or (focused is not None and focused.status != "PASSED"))):
        raise AssentError(
            "A task verdict role cannot PASS while the preceding focused_test failed")
    if not step.writes and record.verdict == "FIXED":
        raise AssentError("A read-only task verdict role cannot return FIXED")

    plan = Plan.parse(cfg.tasks_dir)
    record = auto_fix.validate_review_findings(record, plan)
    wrong_owners = sorted({
        finding.task_id for finding in record.findings
        if finding.task_id != task.id})
    if wrong_owners:
        raise AssentError(
            "A task verdict role may report only its current task: "
            + ", ".join(str(owner) for owner in wrong_owners))

    fresh = parse_task_file(task.path)
    tampered = same_except_status(task, fresh)
    if tampered:
        raise AssentError(
            "Task verdict role modified protected task fields: "
            + ", ".join(tampered))

    additions = tuple(
        auto_fix.ApprovedScopeAddition(
            auto_fix.finding_fingerprint(finding), task.id,
            finding.scope_addition.path, finding.scope_addition.path_state)
        for finding in record.findings
        if finding.scope_addition is not None)
    changed_paths = {
        path.replace("\\", "/")
        for path in gitops.dirty_paths(cfg.root, _task_excludes(cfg, task))
    }
    if changed_paths and (not step.writes or record.verdict != "FIXED"):
        raise AssentError(
            "A task verdict role changed source without writable FIXED authority")
    if record.verdict == "FIXED" and not changed_paths:
        raise AssentError(
            "A task verdict role returned FIXED without changing source")
    if additions:
        if step.writes and record.verdict != "FIXED":
            raise AssentError(
                "A writable task verdict role must repair a scope omission in "
                "the same session and return FIXED")
        auto_fix.validate_scope_additions(
            cfg.root, plan, additions,
            baseline_ref=(session_ref if step.writes else None),
            materialized_new_files=step.writes)
        if step.writes:
            missing = [item.path for item in additions
                       if item.path not in changed_paths]
            if missing:
                raise AssentError(
                    "A same-session task scope amendment did not repair its "
                    "exact path: " + ", ".join(missing))

    outside = _auto_fix_out_of_scope_writes(
        cfg, plan, record, scope_additions=additions,
        since_ref=session_ref)
    if outside:
        raise AssentError(
            "Task verdict role wrote outside its declared or exactly amended "
            "scope: " + ", ".join(outside[:8]))

    state = _with_task_review_record(state, record)
    if additions:
        state = _with_task_scope_transaction(state, fresh, additions)
        write_workflow_state(cfg.tasks_dir, state)
        fresh = _recover_task_scope_amendments(cfg, state, now)
    return fresh, state


def _evaluate_task_workflow_step(
        cfg: Config, task: Task, step: WorkflowTaskStep,
        journal_start: int,
        start_ref: str | None = None,
        session_ref: str | None = None) -> tuple[str, str | None, str]:
    """Accept one non-final role session without completing its task."""
    if not step.writes:
        changed = gitops.changes_outside_scope(
            cfg.root, (), since_ref=session_ref, excludes=_task_excludes(cfg, task))
        if changed:
            return (
                "fail",
                "Read-only workflow role changed source: "
                + ", ".join(changed[:5]),
                "NOT RUN: the role's read-only ability boundary failed.")
    fresh, safety_reason = _inspect_task_safety(cfg, task, start_ref)
    if safety_reason:
        return ("fail", safety_reason,
                "NOT RUN: structural/scope safety failed before step closeout.")
    assert fresh is not None
    worker_entries = [
        item for item in read_entries(task.journal_path)[journal_start:]
        if item.get("by") != "scheduler"]
    if not worker_entries:
        return ("fail", "Workflow session did not append its journal entry",
                "NOT RUN: workflow journal closeout failed before step closeout.")
    if fresh.status == "BLOCKED":
        return ("self_blocked", None,
                "NOT RUN: self-marked BLOCKED closeout skips later task tests.")
    return ("step_done", None,
            "DEFERRED: focused testing runs at an explicit focused_test action "
            "or final task closeout.")


def _evaluate(
        cfg: Config, task: Task, start_ref: str | None = None, *,
        focused_test_record: _TestActionEvidence | None = None,
        focused_test_required: bool = False) -> tuple[str, str | None, str]:
    """Acceptance: structural/scope safety -> status -> verify.

    Returns ``(outcome, reason, focused_evidence)``.

    outcome in {"done", "self_blocked", "fail"}. scope/verify and all fields come from the
    trusted checkpoint version `task`; the on-disk version is only allowed to change the
    status line.
    """
    fresh, safety_reason = _inspect_task_safety(cfg, task, start_ref)
    if safety_reason:
        return ("fail", safety_reason,
                "NOT RUN: structural/scope safety failed before the focused gate.")
    assert fresh is not None

    # status check
    if fresh.status == "BLOCKED":
        return ("self_blocked", None,
                "NOT RUN: self-marked BLOCKED closeout skips the focused gate.")
    if fresh.status != "DONE":
        # Structure and scope are already clean here; the only remaining acceptance gap is
        # the status line. Probe the focused verify once (quietly, so a still-failing verify
        # leaves this path's output byte-for-byte identical to before) to tell a genuine
        # implementation gap apart from a session that simply dropped off before closeout.
        if _run_verify_quiet(cfg, task.verify) == 0:
            return ("fail", _CLOSEOUT_ONLY_REASON_TEMPLATE.format(
                status=fresh.status, verify_command=task.verify),
                f"PASS (closeout probe): {task.verify}")
        return ("fail", f"Status not updated to DONE/BLOCKED (currently {fresh.status})",
                f"FAIL (closeout probe): {task.verify}")

    # The explicit action owns this command when configured. Otherwise retain
    # the legacy final focused check as the task closeout safety floor.
    if focused_test_required:
        if focused_test_record is None or focused_test_record.status != "PASSED":
            evidence = ("NOT PASSED: no focused_test action evidence"
                        if focused_test_record is None else
                        f"{focused_test_record.status}: exit "
                        f"{focused_test_record.exit_code}: {task.verify}")
            return "fail", _focused_test_completion_refusal_for_record(
                focused_test_record), evidence
        focused_evidence = (
            f"PASS (focused_test action reused): exit "
            f"{focused_test_record.exit_code}: {task.verify}")
    else:
        rc = _run_verify(cfg, task.verify)
        focused_evidence = (f"{'PASS' if rc == 0 else 'FAIL'}: exit {rc}: "
                            f"{task.verify}")
        if rc != 0:
            return ("fail", "Verify command exit code is non-zero "
                    f"(={rc}): {task.verify}",
                    focused_evidence)

    # A session handed the bounded shared-path review clause must have run the
    # controlled operation; a source snapshot that is still UNKNOWN or STALE is
    # refused with a precise retry reason rather than closed out.
    try:
        contract = _shared_paths_contract(cfg)
    except AssentError as e:
        return ("fail", f"Shared-path contract could not be classified: {e}",
                focused_evidence)
    refusal = shared_paths.closeout_refusal(contract)
    if refusal:
        return "fail", refusal[:1].upper() + refusal[1:], focused_evidence

    return "done", None, focused_evidence


def _verify_subprocess(cfg: Config, command: str) -> subprocess.CompletedProcess:
    """Run verify in the target working tree and return the completed process (no output).

    A task gate is always a narrow command -- the plan parser refuses one naming
    `.assent/verify.py` -- so the command keeps its original shell semantics and needs
    no main-tree expansion; the cwd is the current target working tree.
    """
    return subprocess.run(
        command, shell=True, cwd=str(cfg.root),
        capture_output=True, encoding="utf-8", errors="replace")


def _run_verify(cfg: Config, command: str) -> int:
    """Run verify in the target working tree; exit code 0 = pass. Echoes the command and
    result (with a failure tail) to stdout."""
    result = _verify_subprocess(cfg, command)
    _show_verify_result(command, result)
    return result.returncode


def _show_verify_result(
        command: str, result: subprocess.CompletedProcess) -> None:
    """Render one already-completed focused command without rerunning it."""
    print(f"  verify: {command}")
    if result.returncode != 0:
        print(f"  verify failed (exit {result.returncode})")
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-8:]
        if tail:
            print("  -- verify output (tail) --")
            for line in tail:
                print(f"  | {line}")
    else:
        print("  verify passed (exit 0)")


_UNITTEST_MODULE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _unittest_modules(command: str) -> tuple[str, ...] | None:
    """Return the modules of an exact ``python -m unittest <modules>`` command.

    The shape is recognized conservatively and nothing else qualifies: a
    different interpreter or runner, extra flags, unusual spacing, or a path
    argument all return ``None``, so such a command keeps running exactly on
    its own as before.
    """
    tokens = command.split(" ")
    if tokens[:3] != ["python", "-m", "unittest"] or len(tokens) < 4:
        return None
    modules = tokens[3:]
    if not all(_UNITTEST_MODULE.match(module) for module in modules):
        return None
    return tuple(modules)


def _merged_unittest_passes(
        cfg: Config,
        commands: list[str]) -> dict[str, subprocess.CompletedProcess]:
    """Prove overlapping unittest commands with one run over their module union.

    Within one fixed tree state, distinct declared commands that name a shared
    module re-import and re-execute that module once per command.  Running the
    ordered union once instead proves every command in the group when it exits
    0.  A nonzero merged run proves nothing and returns nothing, so the caller
    falls back to its ordinary per-command execution and keeps today's failure
    attribution and diagnostics.
    """
    modules_by_command = {command: _unittest_modules(command)
                          for command in commands}
    matched = [command for command in commands
               if modules_by_command[command] is not None]
    if len(matched) < 2:
        return {}
    union: list[str] = []
    for command in matched:
        for module in modules_by_command[command] or ():
            if module not in union:
                union.append(module)
    result = _verify_subprocess(cfg, "python -m unittest " + " ".join(union))
    if result.returncode != 0:
        return {}
    return {command: result for command in matched}


def _verify_focused_locked(
        cfg: Config, task_id: str | None = None, *,
        failure_output: list[str] | None = None) -> int:
    """Run one task check or the DONE-task sweep from the source worktree.

    Focused verification is deliberately separate from receipt-producing full
    verification.  It only proves that the task-level commands pass in the
    folder's own source worktree, so it never creates an integration candidate
    or touches a verification receipt.
    """
    folder = cfg.tasks_name
    main = gitops.main_worktree(cfg.root)
    source = gitops.resolve_folder_source(main, folder, cfg.git_excludes)
    source_cfg = cfg.for_worktree(source.worktree)

    plan = Plan.parse(cfg.tasks_dir)
    if task_id is not None:
        task = plan.get(task_id)
        if task is None:
            raise AssentError(f"task {task_id} not found in folder {folder}")
        commands = [task.verify]
        label = f"verify {folder} --focus {task_id}"
        kind = "focused test"
    else:
        commands = []
        seen: set[str] = set()
        for task in plan.tasks:
            if task.status != "DONE" or task.verify in seen:
                continue
            seen.add(task.verify)
            commands.append(task.verify)
        if not commands:
            raise AssentError(
                f"folder {folder} has no DONE task with an eligible focused "
                "verify command")
        label = f"verify {folder} --focus"
        kind = "focused sweep"

    # --focus provisions the persistent source worktree like every other verify
    # entry point, and writes no receipt of any kind.
    shared_paths.prepare_sources(main, [(folder, source.worktree)])
    print(f"{label}: source worktree {source.worktree}")
    print(f"{label}: {kind} cannot authorize `accept`; "
          "complete integration verification has not run")
    # One fixed tree, so overlapping unittest commands are proven together; a
    # failed or ineligible merge leaves every command to run one at a time.
    merged = (_merged_unittest_passes(source_cfg, commands)
              if task_id is None else {})
    for command in commands:
        proven = merged.get(command)
        if proven is not None:
            _show_verify_result(command, proven)
            continue
        result = _verify_subprocess(source_cfg, command)
        _show_verify_result(command, result)
        if result.returncode != 0:
            if failure_output is not None:
                failure_output.append("\n".join(
                    stream for stream in (result.stdout, result.stderr)
                    if stream))
            print(f"{label}: failed; this focused result cannot "
                  "authorize `accept`")
            return 1
    print(f"{label}: passed; complete integration verification "
          "has not run and this result cannot authorize `accept`")
    return 0


def verify_focused(cfg: Config, task_id: str | None = None) -> int:
    """Run one task check or a DONE-task sweep without making receipts."""
    folder = cfg.tasks_name
    label = (f"verify {folder} --focus {task_id}" if task_id is not None
             else f"verify {folder} --focus")
    try:
        with lockfile.hold_lock(cfg.tasks_dir, folder):
            return _verify_focused_locked(cfg, task_id)
    except lockfile.LockBusy as e:
        print(f"{label}: refused ({e})")
        return 1
    except AssentError as e:
        print(f"{label}: failed ({e})")
        return 1


def _run_verify_quiet(cfg: Config, command: str) -> int:
    """Run verify without printing anything; exit code 0 = pass.

    Used for the closeout-only probe in ``_evaluate`` so that a still-failing verify leaves
    the caller's console output unchanged from before this probe existed.
    """
    return _verify_subprocess(cfg, command).returncode


def _mark_blocked(cfg: Config, task: Task, session: SessionIdentity, reason: str,
                  now: Callable[[], datetime], attempts: int | None = None) -> None:
    """The scheduler marks the task BLOCKED + appends a machine record to the r file + creates a checkpoint.

    The working tree may still hold work that did not pass acceptance (not restored): it is
    gathered into the BLOCKED checkpoint too, so when a human wraps up they can decide in the
    git history whether to keep or change it, and no tokens are wasted.
    """
    set_status(task.path, "BLOCKED")
    detail = (f"Still failed acceptance after {attempts} retries" if attempts
              else "Judged failed without any retry")
    append_entry(task.journal_path, by="scheduler", event="blocked",
                 summary=f"Scheduler marked BLOCKED: {reason}", detail=detail,
                 agent=session.agent,
                 requested_model=session.requested_model,
                 requested_effort=session.requested_effort,
                 time_str=now().isoformat(timespec="seconds"))
    gitops.commit_if_dirty(
        cfg.root, _checkpoint_subject(
            cfg, "auto", task, f"BLOCKED - {_short(reason, 50)}"),
        cfg.git_excludes)


def _quota_wait_seconds(cfg: Config, reset_at: datetime | None,
                        now: Callable[[], datetime]) -> float:
    """How long to wait this round for quota (seconds). If the reset time can be parsed ->
    sleep until reset + buffer (0 if already past); if it cannot -> one poll interval. A pure
    function, easy to test on its own."""
    if reset_at is not None:
        return max(0.0, (reset_at + _QUOTA_BUFFER - now()).total_seconds())
    return float(cfg.quota_poll_minutes * 60)


def _wait_for_quota(cfg: Config, reset_at: datetime | None,
                    sleep: Callable[[float], None],
                    now: Callable[[], datetime]) -> None:
    """Quota bridging: count down in place in the run's own terminal (unix-style \\r overwrites
    the same line)."""
    seconds = _quota_wait_seconds(cfg, reset_at, now)
    if reset_at is not None:
        label = f"Quota resets at {reset_at.astimezone().strftime('%H:%M:%S')}"
    else:
        label = f"Quota poll (every {cfg.quota_poll_minutes} minutes)"
    _countdown(seconds, label, sleep)


def _wait_for_rotation(cfg: Config, sleep: Callable[[float], None]) -> None:
    """Wait one fixed polling interval after every adapter exhausted its quota."""
    seconds = float(cfg.rotation_poll_minutes * 60)
    label = f"Adapter rotation poll (every {cfg.rotation_poll_minutes} minutes)"
    _countdown(seconds, label, sleep)


def _countdown(seconds: float, label: str, sleep: Callable[[float], None], *,
               tick: float = _QUOTA_TICK, segment: float = _COUNTDOWN_SEGMENT,
               stream: TextIO | None = None) -> None:
    """Countdown wait. Terminal (tty) -> update one line in place with \\r, without stacking
    lines; non-tty (redirected to a file/pipe) -> print one message, then sleep in segments of
    at most ``segment`` seconds so a stop request lands within one segment on every platform
    (see _COUNTDOWN_SEGMENT); the total wait is unchanged. The injected sleep lets tests avoid
    really sleeping.

    The segments remain the platform-independent backstop, but the production sleep is
    ``interruptible_sleep``, so a stop request ends the current segment immediately; both
    loops then stop counting down rather than sitting out the rest of a multi-hour wait
    while a KeyboardInterrupt is already pending."""
    if seconds <= 0:
        return
    clear_stop_wake()   # an earlier run's stop request must not shorten this wait
    stream = stream or sys.stdout
    interactive = hasattr(stream, "isatty") and stream.isatty()
    if not interactive:
        stream.write(f"  {label}: waiting about {int(seconds)} seconds before rerunning.\n")
        stream.flush()
        left = seconds
        while left > 0:
            step = segment if segment < left else left
            sleep(step)
            if stop_wake_requested():
                break   # the pending KeyboardInterrupt lands at the next bytecode
            left -= step
        return
    remaining = seconds

    # terminal_log.TeeTextIO provides a terminal-only channel: the countdown is a transient
    # UI and should not be written to _assent.log every second. A test/plain TextIO falls
    # back to write.
    terminal_only = getattr(stream, "write_terminal_only", None)

    def transient_write(text: str) -> None:
        if terminal_only is not None:
            terminal_only(text)
        else:
            stream.write(text)
            stream.flush()
    while remaining > 0:
        h, rem = divmod(int(remaining + 0.999), 3600)
        m, s = divmod(rem, 60)
        transient_write(f"\r  {label}: countdown {h:02d}:{m:02d}:{s:02d} before rerunning... ")
        step = tick if tick < remaining else remaining
        sleep(step)
        if stop_wake_requested():
            break
        remaining -= step
    transient_write("\r" + " " * 48 + "\r")  # clear the countdown line and return the cursor


def _print_summary(plan: Plan) -> None:
    counts = Counter(t.status for t in plan.tasks)
    print("\n===== Run summary =====")
    print(f"DONE: {counts.get('DONE', 0)}  BLOCKED: {counts.get('BLOCKED', 0)}  "
          f"SKIP: {counts.get('SKIP', 0)}  TODO: {counts.get('TODO', 0)}  "
          f"WIP: {counts.get('WIP', 0)}  ({len(plan.tasks)} tasks total)")
    blocked = [t for t in plan.tasks if t.status == "BLOCKED"]
    if blocked:
        print("BLOCKED tasks (handed to a human):")
        for t in blocked:
            print(f"  - {t.id}: {t.title}")
    if counts.get("TODO", 0) == 0 and counts.get("WIP", 0) == 0:
        print("All tasks are DONE/BLOCKED/SKIP.")
