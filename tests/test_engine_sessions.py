"""engine tests for one session round: the adapter call, its process outcome, and the
quota/billing interruptions that keep the task resumable.

Every case here starts (or refuses to start) a scripted AI session and checks what the
scheduler records about it -- the resolved identity in the prompt and journal, an adapter
exit or watchdog stall, a quota round with its wait math, and an exhausted prepaid balance.
The core run loop lives in tests.test_engine; shared fixtures in tests.engine_support.

Chinese literals that remain are deliberate user/upstream passthrough data."""
import _thread
import contextlib
import io
import json
import re
import subprocess
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from assent import AssentError, auto_fix, engine, folder_scheduler, gitops
from assent.adapters import CHECKPOINT_RESUME_RECORD, TaskResult
from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     run_subprocess, wake_stop_waiters)
from assent.config import load_config
from assent.plan import (Plan, append_entry, journal_path_for, parse_task_file,
                         read_entries, read_workflow_state, set_status,
                         workflow_state_path)
from tests.engine_support import (EngineTestCase, ScriptedAdapter, ok_result,
                                  task_text)
from tests.test_contracts import GlobalContractsMixin


class TestBoundedAutoFixSession(GlobalContractsMixin, EngineTestCase):
    @staticmethod
    def review_rounds(count, *names):
        """A workflow with exactly ``count`` verdict-producing review steps.

        The merged reviewer-fixer loop is finite because it walks this list
        position by position, so a case needing N reviewer sessions must
        configure N rounds.
        """
        adapters = list(names) or ["claude"] * count
        rendered = ", ".join(
            item for name in adapters for item in (
                '{ role = "bounded_fixer" }',
                f'{{ role = "folder_reviewer", adapter = {json.dumps(name)} }}'))
        return (
            '\n[abilities.review_fix]\nprompt = "Review and repair."\n'
            'writes = true\ngate = true\nproduces_verdict = true\n'
            '[abilities.fix]\nprompt = "Repair durable findings."\n'
            'writes = true\ngate = false\n'
            '[agents.folder_reviewer]\nability = ["review_fix"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[agents.bounded_fixer]\nability = ["fix"]\n'
            f'[workflow]\nplan = [{rendered}]\n')

    @staticmethod
    def review_session_agents(output):
        """The adapter each review round actually ran under, in order."""
        return re.findall(r"(?m)^Auto-fix review session: (\S+) \|", output)

    def repair_done(self, task_path, files=None, *, requested_model="lite"):
        def step(prompt):
            for rel, content in (files or {}).items():
                path = self.execution_root() / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            fingerprints = tuple(dict.fromkeys(re.findall(
                r"(?m)^- fingerprint: ([0-9a-f]{64})$", prompt)))
            self.assertTrue(fingerprints)
            detail = "\n".join(
                "ASSENT_REPAIR_DISPOSITION " + json.dumps({
                    "fingerprint": fingerprint,
                    "disposition": "fixed",
                    "detail": "The task-focused repair and regression pass.",
                }, separators=(",", ":"), sort_keys=True)
                for fingerprint in fingerprints)
            set_status(task_path, "DONE")
            append_entry(
                journal_path_for(task_path), by="claude",
                requested_model=requested_model, event="done",
                summary="Repair completed", detail=detail)
            return ok_result()
        return step

    @staticmethod
    def recheck_record(finding):
        fingerprint = auto_fix.finding_fingerprint(finding)
        return auto_fix.review_record_json(auto_fix.ReviewRecord("FAIL", (
            auto_fix.ReviewFinding(
                finding.task_id, finding.path, finding.summary,
                finding.evidence, kind=finding.kind,
                recommendation=finding.recommendation,
                scope_addition=finding.scope_addition,
                transition="still_present",
                prior_fingerprint=fingerprint,
                transition_evidence="The repair diff still reproduces the issue."),
        )))

    def test_invalid_record_retry_carries_validator_error_and_can_amend_scope(self):
        task_path = self.write_task(
            1, status="DONE", scope=("src/base.py",))
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        (source / "needed.py").write_text("value = 1\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(retry=1, extra_config=self.review_rounds(2))

        malformed = json.dumps({
            "type": "assent.auto_fix_review",
            "verdict": "FAIL",
            "findings": [{
                "task_id": "t001",
                "path": "src/needed.py",
                "summary": "Required source file was omitted from scope",
                "evidence": (
                    "recommendation: append src/needed.py; scope amendment: "
                    "existing_file"),
            }],
        })
        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py",
            "Required source file was omitted from scope",
            "The task requires the exact existing file.",
            kind="scope_amendment",
            recommendation="Append the exact existing file to t001 scope.",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "existing_file"))

        def corrected(prompt):
            self.assertIn("REVIEW OUTPUT CORRECTION REQUIRED", prompt)
            self.assertIn(
                "Review findings[0] is missing keys: kind, prior_fingerprint, "
                "recommendation, scope_addition, transition, "
                "transition_evidence", prompt)
            self.assertIn("Bounded diagnostic of the rejected output", prompt)
            self.assertIn("recommendation: append src/needed.py", prompt)
            self.assertIn('"scope_addition":{"path":"src/example.py",', prompt)
            self.assertIn('"prior_fingerprint":null', prompt)
            self.assertIn("Review context: COMPLETED_FOLDER", prompt)
            self.assertIn("Source tree:", prompt)
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None)

        reviewer = ScriptedAdapter([
            TaskResult(0, malformed, False, None),
            corrected,
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/needed.py": "value = 2\n"})])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        self.assertNotIn(
            "REVIEW OUTPUT CORRECTION REQUIRED", reviewer.calls[0][0])
        self.assertEqual(parse_task_file(task_path).scope,
                         ["src/base.py", "src/needed.py"])
        self.assertEqual(len(worker.calls), 1)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")

    def test_terminal_invalid_records_write_no_state_or_scope_change(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(retry=1, extra_config=self.review_rounds(1))
        malformed = TaskResult(
            0,
            '{"type":"assent.auto_fix_review","verdict":"FAIL",'
            '"findings":[{"task_id":"t001","path":"src/value.txt",'
            '"summary":"bad","evidence":"missing required keys"}]}',
            False, None)
        reviewer = ScriptedAdapter([malformed, malformed])

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]),
            auto_fix_adapter=reviewer, auto_fix=True), 1)
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(parse_task_file(task_path).scope, ["src/"])
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())
        self.assertFalse(any(
            subject.startswith("wip(plan01/t001): recovered invalid reviewer")
            for subject in self.subjects()))

    def test_review_failure_reopens_repairs_and_reviews_with_the_task_profile(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale", "expected repaired value")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def recheck(prompt):
            self.assertIn("Review stage: RECHECK", prompt)
            self.assertIn(finding.summary, prompt)
            self.assertIn(finding.recommendation, prompt)
            self.assertIn("Worker dispositions:", prompt)
            self.assertIn(auto_fix.finding_fingerprint(finding), prompt)
            self.assertIn("fixed; The task-focused repair and regression pass.",
                          prompt)
            self.assertIn("Approved scope additions:", prompt)
            self.assertIn("Repair-only relevant diff:", prompt)
            self.assertIn("Scheduler-supplied focused evidence:", prompt)
            self.assertIn("Prior observed states:", prompt)
            self.assertIn("must immediately PASS", prompt)
            return TaskResult(0, passed, False, None)

        reviewer = ScriptedAdapter([
            TaskResult(0, failed, False, None),
            recheck,
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "new\n"})])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        # The recheck ran as the second configured round and, having passed,
        # leaves the position on the round that decided it.
        self.assertEqual(state.workflow_step_index, 3)
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 2)
        repair_prompt = worker.calls[0][0]
        self.assertIn("Durable repair brief", repair_prompt)
        self.assertIn(auto_fix.finding_fingerprint(finding), repair_prompt)
        self.assertIn(finding.summary, repair_prompt)
        self.assertIn(finding.evidence, repair_prompt)
        self.assertIn(finding.recommendation, repair_prompt)
        self.assertIn("Focused command evidence", repair_prompt)
        attempt = next(entry for entry in read_entries(journal_path_for(task_path))
                       if entry["event"] == "auto_fix_attempt")
        self.assertEqual((attempt["agent"], attempt["requested_model"],
                          attempt["requested_effort"]),
                         ("claude", "lite", "medium"))

    def test_repair_round_pass_is_reused_by_its_own_recheck_gate(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))
        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale", "expected repaired value")
        command = parse_task_file(task_path).verify
        calls: list[str] = []

        def fake_verify(_cfg, verify_command):
            calls.append(verify_command)
            return subprocess.CompletedProcess(verify_command, 0, "", "")

        def recheck(prompt):
            self.assertIn("Review stage: RECHECK", prompt)
            self.assertIn(f"- PASS (reused authoritative PASS): {command}",
                          prompt)
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)

        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
            recheck,
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "new\n"})])

        out = io.StringIO()
        with mock.patch.object(engine, "_verify_subprocess", fake_verify), \
                contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 0)

        # The initial final gate runs the command, the repair round's closeout
        # gate runs it against the repaired tree, and that round's own recheck
        # reuses that authoritative pass instead of a third execution.
        self.assertEqual(calls, [command, command])
        self.assertEqual(out.getvalue().count("reused authoritative PASS"), 1)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")

    def test_invalid_or_status_incompatible_disposition_reopens_the_task(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(3))
        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale", "review reproduced it")
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
            TaskResult(0, self.recheck_record(finding), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])

        def incompatible(prompt):
            fingerprint = auto_fix.finding_fingerprint(finding)
            self.assertIn(fingerprint, prompt)
            set_status(task_path, "DONE")
            append_entry(
                journal_path_for(task_path), by="claude",
                requested_model="lite", event="done", summary="Not repaired",
                detail=("ASSENT_REPAIR_DISPOSITION " + json.dumps({
                    "fingerprint": fingerprint,
                    "disposition": "still_blocked",
                    "detail": "The reproduced blocker remains.",
                }, separators=(",", ":"), sort_keys=True)))
            return ok_result()

        worker = ScriptedAdapter([
            incompatible,
            self.repair_done(task_path, {"src/value.txt": "fixed\n"}),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(state.workflow_step_index, 5)
        # Every repair keeps the reopened task's own ordinary profile; only the
        # review round position advances.
        self.assertEqual(
            [(model, effort) for _prompt, model, effort in worker.calls],
            [("lite", "medium"), ("lite", "medium")])
        entries = read_entries(journal_path_for(task_path))
        self.assertTrue(any(
            "Repair disposition gate failed" in str(item.get("summary", ""))
            for item in entries if item.get("event") == "auto_fix_blocker"))

    def test_blocked_scope_omission_is_amended_before_fixer_without_handoff(self):
        task_path = self.write_task(
            1, scope=("src/base.py",), status="TODO")
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        (source / "needed.py").write_text("value = 1\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))

        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py", "Required source file was omitted from scope",
            "The blocked worker identified src/needed.py as the exact required edit.",
            kind="scope_amendment",
            recommendation="Append the exact existing file to t001 scope.",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "existing_file"))
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])

        def blocked(_prompt):
            set_status(task_path, "BLOCKED")
            append_entry(
                journal_path_for(task_path), by="claude",
                requested_model="lite", requested_effort="medium",
                event="blocked", summary="Exact source path is outside scope")
            return ok_result()

        worker = ScriptedAdapter([
            blocked,
            self.repair_done(task_path, {"src/needed.py": "value = 2\n"}),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)

        task = parse_task_file(task_path)
        self.assertEqual(task.status, "DONE")
        self.assertEqual(task.scope, ["src/base.py", "src/needed.py"])
        entries = read_entries(journal_path_for(task_path))
        amendment = next(
            item for item in entries
            if item["event"] == "auto_fix_scope_amendment")
        self.assertIn("task contract before sha256", amendment["detail"])
        self.assertIn("task plan after sha256", amendment["detail"])
        self.assertLess(
            next(index for index, item in enumerate(entries)
                 if item["event"] == "auto_fix_scope_amendment"),
            next(index for index, item in enumerate(entries)
                 if item["event"] == "rework_requested"))
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertTrue(state.plan_digest_transitions)
        self.assertEqual(len(worker.calls), 2)
        repair_prompt = worker.calls[1][0]
        self.assertIn(auto_fix.finding_fingerprint(finding), repair_prompt)
        self.assertIn(finding.summary, repair_prompt)
        self.assertIn(finding.evidence, repair_prompt)
        self.assertIn(finding.recommendation, repair_prompt)
        self.assertIn("Exact source path is outside scope", repair_prompt)
        self.assertIn("NOT RUN: self-marked BLOCKED", repair_prompt)
        self.assertIn("src/needed.py (existing_file)", repair_prompt)

    def test_recheck_approved_scope_addition_is_applied_before_its_fixer(self):
        task_path = self.write_task(1, status="DONE", scope=("src/base.py",))
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("old\n", encoding="utf-8")
        (source / "needed.py").write_text("value = 1\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(3))

        stale = auto_fix.ReviewFinding(
            "t001", "src/base.py", "base value is stale",
            "The review reproduced the stale value.")
        exposed = auto_fix.ReviewFinding(
            "t001", "src/needed.py",
            "Required source file was omitted from scope",
            "The repair diff proves the remaining fix lives in src/needed.py.",
            kind="scope_amendment",
            recommendation="Append the exact existing file to t001 scope.",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "existing_file"),
            transition="newly_exposed",
            transition_evidence=(
                "t001 acceptance requires the repaired value that only "
                "src/needed.py can supply."))
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (stale,))), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (exposed,))), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])

        def amended_repair(prompt):
            # The recheck-approved path is already part of the trusted contract
            # this fixer receives, so writing it stays inside the scope check.
            self.assertIn("src/needed.py (existing_file)", prompt)
            self.assertIn("src/needed.py", parse_task_file(task_path).scope)
            return self.repair_done(
                task_path, {"src/needed.py": "value = 2\n"})(prompt)

        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/base.py": "new\n"}),
            amended_repair,
        ])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        self.assertEqual(parse_task_file(task_path).scope,
                         ["src/base.py", "src/needed.py"])
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(len(worker.calls), 2)
        events = [item["event"]
                  for item in read_entries(journal_path_for(task_path))]
        self.assertEqual(events.count("auto_fix_scope_amendment"), 1)
        self.assertLess(
            events.index("auto_fix_scope_amendment"),
            len(events) - 1 - events[::-1].index("rework_requested"))

    def test_scope_amendment_resumes_after_task_write_before_state_write(self):
        task_path = self.write_task(
            1, scope=("src/base.py",), status="BLOCKED")
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        (source / "needed.py").write_text("value = 1\n", encoding="utf-8")
        cfg = self.build(extra_config=self.review_rounds(1))
        plan = Plan.parse(cfg.tasks_dir)
        contracts = engine._task_contract_snapshots(plan)
        plan_digest = engine._contracts_digest(plan, contracts)
        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py", "Scope omitted an exact source file",
            "Durable worker evidence names the required source file.",
            kind="scope_amendment",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "existing_file"))
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (finding,)),
            source_tree="1" * 40, task_plan_sha256=plan_digest,
            review_prompt_sha256="2" * 64,
            reviewer_adapter="claude", reviewer_model="prime",
            reviewer_effort="heavy", review_context="blocked_adjudication",
            failure_trigger="worker_blocked")
        state_path = auto_fix.auto_fix_state_path(cfg)
        auto_fix.write_auto_fix_state(state_path, state)
        now = lambda: datetime(2026, 8, 3, tzinfo=timezone.utc)

        task_before = task_path.read_bytes()
        state_before = state_path.read_bytes()
        real_access = engine.os.access
        with mock.patch(
                "assent.engine.os.access",
                side_effect=lambda path, mode: (
                    False if Path(path) == journal_path_for(task_path).parent
                    else real_access(path, mode))):
            with self.assertRaisesRegex(AssentError, "not writable"):
                engine._apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts, now)
        self.assertEqual(task_path.read_bytes(), task_before)
        self.assertEqual(state_path.read_bytes(), state_before)

        real_write_state = auto_fix.write_auto_fix_state
        state_writes = 0

        def fail_after_transaction_record(path, value):
            nonlocal state_writes
            state_writes += 1
            if state_writes == 2:
                raise AssentError("injected state boundary")
            return real_write_state(path, value)

        with mock.patch(
                "assent.engine.auto_fix.write_auto_fix_state",
                side_effect=fail_after_transaction_record):
            with self.assertRaisesRegex(AssentError, "injected state boundary"):
                engine._apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts, now)
        self.assertIn("src/needed.py", parse_task_file(task_path).scope)
        self.assertFalse(any(
            item.get("event") == "auto_fix_scope_amendment"
            for item in read_entries(journal_path_for(task_path))))

        recovered = auto_fix.read_auto_fix_state(state_path)
        with mock.patch(
                "assent.engine.append_entry",
                side_effect=AssentError("injected journal boundary")):
            with self.assertRaisesRegex(AssentError, "injected journal boundary"):
                engine._apply_reviewed_scope_amendments(
                    cfg, recovered, Plan.parse(cfg.tasks_dir),
                    engine._task_contract_snapshots(
                        Plan.parse(cfg.tasks_dir)), now)
        recovered = auto_fix.read_auto_fix_state(state_path)
        self.assertEqual(recovered.task_plan_sha256,
                         recovered.plan_digest_transitions[-1].after_sha256)
        self.assertFalse(any(
            item.get("event") == "auto_fix_scope_amendment"
            for item in read_entries(journal_path_for(task_path))))

        recovered, _plan, _contracts = engine._apply_reviewed_scope_amendments(
            cfg, recovered, Plan.parse(cfg.tasks_dir),
            engine._task_contract_snapshots(Plan.parse(cfg.tasks_dir)), now)
        entries = [
            item for item in read_entries(journal_path_for(task_path))
            if item.get("event") == "auto_fix_scope_amendment"]
        self.assertEqual(len(entries), 1)

        engine._apply_reviewed_scope_amendments(
            cfg, recovered, Plan.parse(cfg.tasks_dir),
            engine._task_contract_snapshots(Plan.parse(cfg.tasks_dir)), now)
        entries = [
            item for item in read_entries(journal_path_for(task_path))
            if item.get("event") == "auto_fix_scope_amendment"]
        self.assertEqual(len(entries), 1)

    def test_scope_amendment_resumes_after_one_of_two_journals(self):
        first_path = self.write_task(
            1, scope=("src/base.py",), status="BLOCKED")
        second_path = self.write_task(
            2, scope=("tests/base.py",), status="BLOCKED")
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "base.py").write_text("base = 1\n", encoding="utf-8")
        (self.root / "tests" / "base.py").write_text("base = 1\n", encoding="utf-8")
        (self.root / "src" / "needed.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "tests" / "needed.py").write_text("value = 1\n", encoding="utf-8")
        cfg = self.build(extra_config=self.review_rounds(1))
        plan = Plan.parse(cfg.tasks_dir)
        contracts = engine._task_contract_snapshots(plan)
        plan_digest = engine._contracts_digest(plan, contracts)
        findings = (
            auto_fix.ReviewFinding(
                "t001", "src/needed.py", "First exact scope omission",
                "Durable evidence names src/needed.py.", kind="scope_amendment",
                scope_addition=auto_fix.ScopeAddition(
                    "src/needed.py", "existing_file")),
            auto_fix.ReviewFinding(
                "t002", "tests/needed.py", "Second exact scope omission",
                "Durable evidence names tests/needed.py.", kind="scope_amendment",
                scope_addition=auto_fix.ScopeAddition(
                    "tests/needed.py", "existing_file")),
        )
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", findings), source_tree="1" * 40,
            task_plan_sha256=plan_digest, review_prompt_sha256="2" * 64,
            reviewer_adapter="claude", reviewer_model="prime",
            reviewer_effort="heavy", review_context="blocked_adjudication",
            failure_trigger="worker_blocked")
        state_path = auto_fix.auto_fix_state_path(cfg)
        auto_fix.write_auto_fix_state(state_path, state)
        now = lambda: datetime(2026, 8, 3, tzinfo=timezone.utc)
        real_append = engine.append_entry
        appends = 0

        def fail_after_first_journal(*args, **kwargs):
            nonlocal appends
            appends += 1
            if appends == 2:
                raise AssentError("injected second journal boundary")
            return real_append(*args, **kwargs)

        with mock.patch(
                "assent.engine.append_entry", side_effect=fail_after_first_journal):
            with self.assertRaisesRegex(AssentError, "second journal boundary"):
                engine._apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts, now)

        recovered = auto_fix.read_auto_fix_state(state_path)
        self.assertEqual(len(recovered.scope_amendments), 2)
        self.assertEqual(recovered.task_plan_sha256,
                         recovered.plan_digest_transitions[-1].after_sha256)
        engine._apply_reviewed_scope_amendments(
            cfg, recovered, Plan.parse(cfg.tasks_dir),
            engine._task_contract_snapshots(Plan.parse(cfg.tasks_dir)), now)
        self.assertIn("src/needed.py", parse_task_file(first_path).scope)
        self.assertIn("tests/needed.py", parse_task_file(second_path).scope)
        amendment_entries = [
            item for path in (first_path, second_path)
            for item in read_entries(journal_path_for(path))
            if item.get("event") == "auto_fix_scope_amendment"]
        self.assertEqual(len(amendment_entries), 2)

    def test_directory_scoped_focused_failure_persists_and_starts_fixer(self):
        verify = ('python -c "import pathlib,sys;sys.exit('
                  "0 if pathlib.Path('src/ok.txt').exists() else 1)\"")
        task_path = self.write_task(
            1, status="DONE", scope=("src/",), verify=verify)
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(1))

        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/ok.txt": "ready\n"})])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        focused = next(
            finding for finding in state.findings
            if finding.summary == "Final focused verification failed")
        self.assertEqual(focused.path, "src")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(len(reviewer.calls), 1)

    def test_a_repeated_finding_keeps_the_task_profile_and_advances_the_round(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(3))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "still stale", "review reproduced the issue")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([
            TaskResult(0, failed, False, None),
            TaskResult(0, self.recheck_record(finding), False, None),
            TaskResult(0, passed, False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "partial\n"}),
            self.repair_done(task_path, {"src/value.txt": "fixed\n"}),
        ])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        # No worker-identity ladder remains: the same finding twice is repaired
        # twice under t001's ordinary profile, and only the round index moves.
        self.assertEqual([(model, effort) for _prompt, model, effort in worker.calls],
                         [("lite", "medium"), ("lite", "medium")])
        self.assertEqual(state.workflow_step_index, 5)

    def test_multi_task_finding_round_gives_every_task_the_normal_profile(self):
        first = self.write_task(
            1, status="DONE", scope=("src/one.txt",))
        second = self.write_task(
            2, status="DONE", scope=("src/two.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "one.txt").write_text("old one\n", encoding="utf-8")
        (source / "two.txt").write_text("old two\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))
        failed = auto_fix.review_record_json(auto_fix.ReviewRecord("FAIL", (
            auto_fix.ReviewFinding(
                "t001", "src/one.txt", "first blocker", "first evidence"),
            auto_fix.ReviewFinding(
                "t002", "src/two.txt", "second blocker", "second evidence"),
        )))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([
            TaskResult(0, failed, False, None),
            TaskResult(0, passed, False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(first, {"src/one.txt": "new one\n"}),
            self.repair_done(second, {"src/two.txt": "new two\n"}),
        ])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        self.assertEqual(
            [(model, effort) for _prompt, model, effort in worker.calls],
            [("lite", "medium"), ("lite", "medium")])
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(state.workflow_step_index, 3)

    def test_dependency_cascade_shares_the_same_normal_repair_round(self):
        first = self.write_task(
            1, status="DONE", scope=("src/base.txt",))
        second = self.write_task(
            2, status="DONE", deps=("t001",), scope=("src/dependent.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "base.txt").write_text("old base\n", encoding="utf-8")
        (source / "dependent.txt").write_text(
            "old dependent\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))
        failed = auto_fix.review_record_json(auto_fix.ReviewRecord("FAIL", (
            auto_fix.ReviewFinding(
                "t001", "src/base.txt", "base blocker", "base evidence"),
        )))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([
            TaskResult(0, failed, False, None),
            TaskResult(0, passed, False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(first, {"src/base.txt": "new base\n"}),
            self.repair_done(second, {"src/dependent.txt": "new dependent\n"}),
        ])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        self.assertEqual(
            [(model, effort) for _prompt, model, effort in worker.calls],
            [("lite", "medium"), ("lite", "medium")])
        self.assertEqual(parse_task_file(second).status, "DONE")
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(state.workflow_step_index, 3)

    def test_interrupted_multi_task_round_resumes_on_the_same_profile(self):
        first = self.write_task(
            1, status="DONE", scope=("src/one.txt",))
        second = self.write_task(
            2, status="DONE", scope=("src/two.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "one.txt").write_text("old one\n", encoding="utf-8")
        (source / "two.txt").write_text("old two\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))
        failed = auto_fix.review_record_json(auto_fix.ReviewRecord("FAIL", (
            auto_fix.ReviewFinding(
                "t001", "src/one.txt", "first blocker", "first evidence"),
            auto_fix.ReviewFinding(
                "t002", "src/two.txt", "second blocker", "second evidence"),
        )))
        reviewer = ScriptedAdapter([
            TaskResult(0, failed, False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])

        def interrupt_first(_prompt):
            (self.execution_root() / "src" / "one.txt").write_text(
                "partial one\n", encoding="utf-8")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([interrupt_first]),
            auto_fix_adapter=reviewer, auto_fix=True), 130)
        interrupted = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(interrupted.phase, "REPAIRING")
        self.assertEqual(interrupted.workflow_step_index, 3)

        resumed = ScriptedAdapter([
            self.repair_done(first, {"src/one.txt": "new one\n"}),
            self.repair_done(second, {"src/two.txt": "new two\n"}),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=resumed, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        # Nothing was consumed by the interrupted round, so the resumed one
        # repairs under exactly the same ordinary identity.
        self.assertEqual(
            [(model, effort) for _prompt, model, effort in resumed.calls],
            [("lite", "medium"), ("lite", "medium")])
        completed = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(completed.verdict, "PASS")
        self.assertEqual(completed.workflow_step_index, 3)

    def test_process_creation_failure_leaves_the_same_repair_resumable(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.")
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])

        def process_creation_failed(_prompt):
            run_subprocess(
                [str(self.root / "missing-auto-fix-adapter.exe")],
                self.execution_root(), 0)

        worker = ScriptedAdapter([
            process_creation_failed,
            self.repair_done(task_path, {"src/value.txt": "new\n"}),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 1)

        failed_start = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(failed_start.phase, "REPAIRING")
        self.assertEqual(failed_start.workflow_step_index, 3)
        self.assertEqual(parse_task_file(task_path).status, "TODO")
        entries = read_entries(journal_path_for(task_path))
        self.assertEqual(sum(
            item.get("event") == "rework_requested" for item in entries), 1)
        self.assertFalse(any(
            item.get("event") in {"interrupt", "auto_fix_blocker"}
            for item in entries))
        self.assertEqual(failed_start.current_finding_fingerprints,
                         (auto_fix.finding_fingerprint(finding),))

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        completed = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(completed.verdict, "PASS")
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(
            [(model, effort) for _prompt, model, effort in worker.calls],
            [("lite", "medium"), ("lite", "medium")])
        entries = read_entries(journal_path_for(task_path))
        self.assertEqual(sum(
            item.get("event") == "rework_requested" for item in entries), 1)
        self.assertEqual(sum(
            item.get("event") == "auto_fix_attempt" for item in entries), 2)

    def test_post_start_oserror_keeps_progress_and_reuses_the_profile(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.")
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])

        def failed_after_start(_prompt):
            marker = self.execution_root() / "src" / "started.txt"
            marker.write_text("child started\n", encoding="utf-8")
            raise OSError("output collection failed after child start")

        worker = ScriptedAdapter([
            failed_after_start,
            self.repair_done(task_path, {"src/value.txt": "new\n"}),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 1)
        attempted = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(attempted.phase, "REPAIRING")
        self.assertEqual(parse_task_file(task_path).status, "WIP")

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        completed = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(completed.verdict, "PASS")
        self.assertEqual(
            [(model, effort) for _prompt, model, effort in worker.calls],
            [("lite", "medium"), ("lite", "medium")])

    def unresolved_folder(self, rounds=2, *, status="DONE"):
        """A folder whose configured rounds all end on the same open blocker.

        No round ever repairs the finding, so the list runs out with a question
        the scheduler cannot decide -- the exact REVIEW UNRESOLVED hand-off
        point.  `status` starts the task TODO when the case needs the ordinary
        worker session before the review rounds.
        """
        task_path = self.write_task(1, status=status, scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(rounds))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "persistent blocker", "still reproducible")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        reviewer = ScriptedAdapter(
            [TaskResult(0, failed, False, None)]
            + [TaskResult(0, self.recheck_record(finding), False, None)
               for _ in range(rounds - 1)])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": f"attempt {index}\n"})
            for index in range(1, rounds + 1)])
        return cfg, task_path, reviewer, worker, finding

    def test_round_list_exhaustion_settles_as_an_unresolved_human_decision(self):
        cfg, task_path, reviewer, worker, finding = self.unresolved_folder()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 0)
        # Both configured rounds ran, so no third reviewer session starts; the
        # open finding is a question the scheduler cannot decide, not a run
        # failure, so it exits zero and the task keeps the status its own
        # closeout gave it.
        self.assertEqual(len(reviewer.calls), 2)
        # Only the writable workflow step between the two verdict steps can
        # consume the first verdict's durable finding.
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertIn("REVIEW UNRESOLVED, HUMAN DECISION", out.getvalue())

        fingerprint = auto_fix.finding_fingerprint(finding)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.workflow_step_index, 4)
        self.assertEqual(state.current_finding_fingerprints, (fingerprint,))
        outcome = state.unresolved_review
        self.assertIsNotNone(outcome)
        review = cfg.workflow_plan[3]
        self.assertEqual(
            (outcome.round_index, outcome.rounds_used, outcome.adapter,
             outcome.model, outcome.effort, outcome.finding_fingerprints),
            (3, 4, review.adapter, review.requested_model,
             review.requested_effort, (fingerprint,)))
        self.assertIsNone(state.self_fixed_unreviewed)

        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn(
            "Folder auto-fix: REVIEW UNRESOLVED, HUMAN DECISION (fresh)",
            report)
        self.assertIn("Terminal: REVIEW UNRESOLVED, HUMAN DECISION (round 4 "
                      f"of 4, {review.adapter}/{review.requested_model}/"
                      f"{review.requested_effort}", report)
        self.assertIn(fingerprint, report)

        entries = read_entries(journal_path_for(task_path))
        self.assertFalse(any(
            item.get("event") == "auto_fix_exhausted" for item in entries))
        settled = [item for item in entries
                   if item.get("event") == "auto_fix_unresolved_review"]
        self.assertEqual(len(settled), 1)
        self.assertIn("still unresolved", settled[0]["summary"])
        self.assertIn(fingerprint, settled[0]["detail"])
        # Nothing was reverted, reopened, or marked BLOCKED to reach that exit
        # code: every status is exactly what its own closeout wrote.
        self.assertFalse(any(
            item.get("event") == "blocked" for item in entries))

    def test_a_settled_unresolved_folder_is_terminal_on_the_next_run(self):
        cfg, task_path, reviewer, worker, _finding = self.unresolved_folder()
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        state_path = auto_fix.auto_fix_state_path(cfg)
        settled = auto_fix.read_auto_fix_state(state_path)

        # A settled outcome is not a resumable phase: the restart reopens,
        # reviews and runs nothing, and spends no reviewer session.
        restarted = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=restarted,
                auto_fix=True), 0)
        self.assertEqual(restarted.calls, [])
        self.assertEqual(auto_fix.read_auto_fix_state(state_path), settled)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertIn("REVIEW UNRESOLVED, HUMAN DECISION", out.getvalue())
        self.assertEqual(len([
            item for item in read_entries(journal_path_for(task_path))
            if item.get("event") == "auto_fix_unresolved_review"]), 1)

        # Terminal for an ordinary run too: unlike an unconfirmed FAIL it must
        # not refuse closeout.
        ordinary = io.StringIO()
        with contextlib.redirect_stdout(ordinary):
            self.assertEqual(engine.run(cfg, adapter=ScriptedAdapter([])), 0)
        self.assertNotIn("closeout refused", ordinary.getvalue())
        self.assertEqual(auto_fix.read_auto_fix_state(state_path), settled)

    def test_unresolved_state_and_report_survive_a_later_closeout_failure(self):
        cfg, task_path, reviewer, worker, _finding = self.unresolved_folder()

        def explode(*_args, **_kwargs):
            raise AssentError("journal write failed after the settled outcome")

        out = io.StringIO()
        with mock.patch.object(
                engine, "_auto_fix_journal_unresolved_review", explode), \
                contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 1)

        # The step after the durable write and the report refresh failed, and
        # both were already on disk -- the same try/finally guarantee folder
        # verification's closeout proves for its own report.
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNotNone(state.unresolved_review)
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn(
            "Folder auto-fix: REVIEW UNRESOLVED, HUMAN DECISION (fresh)",
            report)
        self.assertFalse(any(
            item.get("event") == "auto_fix_unresolved_review"
            for item in read_entries(journal_path_for(task_path))))

    def add_folder(self, name, **kw):
        """A second, independent work folder in the same project."""
        directory = self.root / ".assent" / name
        directory.mkdir()
        path = directory / "t001_task.e.toml"
        path.write_text(task_text(**kw), encoding="utf-8", newline="\n")
        return path

    def run_folder_queue(self, adapters):
        """Drive the real `run --all` folder scheduler over this project.

        Nothing about the launch decision is mocked: the dependency graph, the
        `while not failure and runnable ...` launch loop, and its fail-stop flag
        are the production ones, and each folder's exit code is what its own
        real `engine.run` returns.  Only the child-process boundary is
        substituted -- the same stand-in technique tests.test_folder_scheduler
        uses -- because a real child would have to reach an actual AI CLI.
        """
        config_path = str(self.root / ".assent" / "assent.toml")
        started: list[str] = []

        class FolderRun:
            def __init__(self, folder):
                self.folder = folder
                self.code = None

            def poll(self):
                if self.code is None:
                    self.code = engine.run(
                        load_config(config_path, self.folder), auto_fix=True)
                return self.code

        def start(_config_path, folder, *, auto_fix=False):
            started.append(folder)
            return FolderRun(folder)

        out = io.StringIO()
        with mock.patch.object(folder_scheduler, "_start_folder", start), \
                mock.patch.object(
                    engine, "get_adapter",
                    side_effect=lambda _name, cfg: adapters[cfg.tasks_name]), \
                contextlib.redirect_stdout(out):
            code = folder_scheduler.run_all(
                config_path, self.root / ".assent", 1, auto_fix=True)
        return code, started, out.getvalue()

    def unresolved_queue_adapter(self, task_path, reviewer, worker):
        """One folder's whole session script: task, review, repair, review, repair."""
        return ScriptedAdapter([
            self.ai_done(task_path), reviewer.steps[0], worker.steps[0],
            reviewer.steps[1], worker.steps[1]])

    def test_an_unresolved_folder_still_lets_the_next_folder_start(self):
        second_task = self.add_folder("plan02", scope=("other/",))
        cfg, task_path, reviewer, worker, _finding = self.unresolved_folder(
            status="TODO")
        adapters = {
            "plan01": self.unresolved_queue_adapter(
                task_path, reviewer, worker),
            "plan02": ScriptedAdapter([
                self.ai_done(second_task),
                TaskResult(0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("PASS", ())), False, None)]),
        }

        code, started, output = self.run_folder_queue(adapters)

        # The launch loop stops launching once any folder exits nonzero, so an
        # unresolved review must not exit nonzero: plan02 is queued behind
        # plan01 and still runs to completion in the same invocation.
        self.assertEqual(code, 0)
        self.assertEqual(started, ["plan01", "plan02"])
        self.assertIn("REVIEW UNRESOLVED, HUMAN DECISION", output)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertEqual(parse_task_file(second_task).status, "DONE")
        first = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNotNone(first.unresolved_review)
        second = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(
            load_config(str(self.root / ".assent" / "assent.toml"), "plan02")))
        self.assertEqual(second.verdict, "PASS")

    def test_an_unrelated_folder_failure_in_the_same_run_still_exits_nonzero(self):
        self.add_folder("plan02", scope=("other/",))
        cfg, task_path, reviewer, worker, _finding = self.unresolved_folder(
            status="TODO")

        class UnavailableAdapter(ScriptedAdapter):
            def resolve_model(self, model):
                raise AssentError("adapter claude is not installed")

        adapters = {
            "plan01": self.unresolved_queue_adapter(
                task_path, reviewer, worker),
            "plan02": UnavailableAdapter([]),
        }

        code, started, output = self.run_folder_queue(adapters)

        # The settled outcome removes exactly one cause of a nonzero exit; a
        # genuine failure in the same invocation is still reported as one.
        self.assertEqual(code, 1)
        self.assertEqual(started, ["plan01", "plan02"])
        self.assertIn("Work folder failed: plan02", output)
        self.assertIsNotNone(auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg)).unresolved_review)

    def gate_log(self):
        """An absolute log a real focused gate appends to, outside every worktree."""
        log = self.root.parent / f"{self.root.name}.gatelog"
        self.addCleanup(log.unlink, True)
        return log

    @staticmethod
    def recording_gate(log, *, fails_on=None):
        """A real verify command that records the source value it was run against.

        Nothing here is mocked, so each recorded line is proof the scheduler
        really executed the task's own gate, and the value it read is proof of
        which tree that execution proved.
        """
        check = (f"sys.exit(3 if value.strip() == {fails_on!r} else 0)"
                 if fails_on is not None else "sys.exit(0)")
        return ('python -c "import pathlib,sys;'
                "value=pathlib.Path('src/value.txt').read_text(encoding='utf-8');"
                f"log=pathlib.Path(r'{log}');"
                "prev=log.read_text(encoding='utf-8') if log.exists() else '';"
                "log.write_text(prev+value,encoding='utf-8');"
                f'{check}"')

    @staticmethod
    def gate_runs(log):
        """The source value every recorded gate execution saw, in order."""
        if not log.exists():
            return []
        return log.read_text(encoding="utf-8").splitlines()

    def self_fixed_folder(self, rounds, *, verify=None, repairs=None):
        """A folder whose every configured round repairs and none confirms.

        The last round leaves a FIXED verdict with no round left to review it,
        which is the exact SELF-FIXED, UNREVIEWED hand-off point.  `verify`
        declares the task's own focused gate and `repairs` what each round
        writes, which is what the settling gate is judged on.
        """
        gate = {"verify": verify} if verify is not None else {}
        task_path = self.write_task(1, status="DONE", scope=("src/",), **gate)
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(rounds))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.",
            recommendation="Write the repaired value.")
        repaired = auto_fix.ReviewFinding(
            finding.task_id, finding.path, finding.summary, finding.evidence,
            kind=finding.kind, recommendation=finding.recommendation,
            transition="still_present",
            prior_fingerprint=auto_fix.finding_fingerprint(finding),
            transition_evidence="The same blocker was repaired again in place.")

        def fix(position):
            def step(_prompt):
                text = (repairs[position - 1] if repairs is not None
                        else f"repair {position}")
                (self.execution_root() / "src" / "value.txt").write_text(
                    f"{text}\n", encoding="utf-8")
                return TaskResult(0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord(
                        "FIXED", (finding if position == 1 else repaired,))),
                    False, None)
            return step

        reviewer = ScriptedAdapter([fix(index + 1) for index in range(rounds)])
        return cfg, task_path, reviewer, finding

    def test_exhausted_rounds_settle_self_fixed_unreviewed_and_keep_done(self):
        cfg, task_path, reviewer, finding = self.self_fixed_folder(2)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
                auto_fix=True), 0)

        # The code passed its own focused gate, so nothing is BLOCKED: only
        # independent review confirmation is missing.
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertEqual(len(reviewer.calls), 2)
        self.assertIn("SELF-FIXED, UNREVIEWED", out.getvalue())

        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        outcome = state.self_fixed_unreviewed
        self.assertIsNotNone(outcome)
        review = cfg.workflow_plan[3]
        self.assertEqual(
            (outcome.round_index, outcome.rounds_used, outcome.adapter,
             outcome.model, outcome.effort, outcome.finding_fingerprints),
            (3, 4, review.adapter, review.requested_model,
             review.requested_effort,
             (auto_fix.finding_fingerprint(finding),)))
        # The settled record is rebound to the tree the last round's repair was
        # checkpointed into, which is the source a human now reads.
        self.assertEqual(state.source_tree,
                         gitops.tree_of(self.execution_root(), "HEAD"))

        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("Folder auto-fix: SELF-FIXED, UNREVIEWED (fresh)", report)
        self.assertIn(
            f"Self-fixed round: 4 of 4 ({review.adapter}/"
            f"{review.requested_model}/{review.requested_effort})", report)
        self.assertIn("Terminal: SELF-FIXED, UNREVIEWED", report)

        settled = [item for item in read_entries(journal_path_for(task_path))
                   if item.get("event") == "auto_fix_self_fixed_unreviewed"]
        self.assertEqual(len(settled), 1)
        self.assertIn("no further configured round confirmed it",
                      settled[0]["summary"])
        self.assertIn(auto_fix.finding_fingerprint(finding),
                      settled[0]["detail"])

    def test_a_settled_self_fixed_folder_is_terminal_on_the_next_run(self):
        cfg, task_path, reviewer, _finding = self.self_fixed_folder(2)
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        state_path = auto_fix.auto_fix_state_path(cfg)
        settled = auto_fix.read_auto_fix_state(state_path)

        # A settled outcome is not a resumable phase: the restart reopens,
        # reviews and runs nothing, and spends no reviewer session.
        restarted = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=restarted,
                auto_fix=True), 0)
        self.assertEqual(restarted.calls, [])
        self.assertEqual(auto_fix.read_auto_fix_state(state_path), settled)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertIn("SELF-FIXED, UNREVIEWED", out.getvalue())
        self.assertEqual(len([
            item for item in read_entries(journal_path_for(task_path))
            if item.get("event") == "auto_fix_self_fixed_unreviewed"]), 1)

        # A settled outcome is the one non-PASS state that still closes out an
        # ordinary run: it is terminal, so unlike an unconfirmed FIXED it must
        # not refuse closeout.
        ordinary = io.StringIO()
        with contextlib.redirect_stdout(ordinary):
            self.assertEqual(engine.run(cfg, adapter=ScriptedAdapter([])), 0)
        self.assertNotIn("closeout refused", ordinary.getvalue())
        self.assertEqual(auto_fix.read_auto_fix_state(state_path), settled)

    def test_self_fixed_state_and_report_survive_a_later_closeout_failure(self):
        cfg, task_path, reviewer, _finding = self.self_fixed_folder(2)

        def explode(*_args, **_kwargs):
            raise AssentError("journal write failed after the settled outcome")

        out = io.StringIO()
        with mock.patch.object(engine, "_auto_fix_journal_self_fixed", explode), \
                contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
                auto_fix=True), 1)

        # The step after the durable write and the report refresh failed, and
        # both were already on disk -- the same try/finally guarantee folder
        # verification's closeout proves for its own report.
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNotNone(state.self_fixed_unreviewed)
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("Folder auto-fix: SELF-FIXED, UNREVIEWED (fresh)", report)
        self.assertFalse(any(
            item.get("event") == "auto_fix_self_fixed_unreviewed"
            for item in read_entries(journal_path_for(task_path))))

    def test_the_settling_gate_proves_the_final_rounds_repair(self):
        log = self.gate_log()
        cfg, task_path, reviewer, _finding = self.self_fixed_folder(
            2, verify=self.recording_gate(log))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
                auto_fix=True), 0)

        # Each round gates the folder before its own review, so those two runs
        # see the source as it was before that round repaired it.  The third
        # run is the settling gate, and it is the only one that ever reads the
        # final round's repair.
        self.assertEqual(self.gate_runs(log), ["old", "repair 1", "repair 2"])
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertIn("SELF-FIXED, UNREVIEWED", out.getvalue())

        command = parse_task_file(task_path).verify
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNotNone(state.self_fixed_unreviewed)
        brief = next(item for item in state.repair_briefs
                     if item.task_id == "t001")
        self.assertIn(f"- PASS t001: {command}", brief.brief)

        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("Folder auto-fix: SELF-FIXED, UNREVIEWED (fresh)", report)
        self.assertIn("Settling focused gate evidence", report)
        settled = next(item for item in read_entries(journal_path_for(task_path))
                       if item.get("event") == "auto_fix_self_fixed_unreviewed")
        self.assertIn(f"- PASS t001: {command}", settled["detail"])

    def test_a_final_repair_failing_its_focused_gate_does_not_settle(self):
        log = self.gate_log()
        cfg, task_path, reviewer, _finding = self.self_fixed_folder(
            2, verify=self.recording_gate(log, fails_on="broken"),
            repairs=("repair 1", "broken"))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
                auto_fix=True), 1)

        self.assertEqual(self.gate_runs(log), ["old", "repair 1", "broken"])
        self.assertIn("was not proven by the focused gate", out.getvalue())
        # A repair the task's own gate rejects is not an acceptable outcome, so
        # the folder does not settle -- and nothing is reverted or re-marked to
        # achieve that: the status, the edit and the evidence all stay put.
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNone(state.self_fixed_unreviewed)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertEqual(
            (self.execution_root() / "src" / "value.txt").read_text(
                encoding="utf-8"),
            "broken\n")
        self.assertFalse(any(
            item.get("event") == "auto_fix_self_fixed_unreviewed"
            for item in read_entries(journal_path_for(task_path))))

        command = parse_task_file(task_path).verify
        brief = next(item for item in state.repair_briefs
                     if item.task_id == "t001")
        self.assertIn(f"- FAIL (3) t001: {command}", brief.brief)
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("Settling focused gate evidence", report)
        self.assertNotIn("SELF-FIXED, UNREVIEWED", report)
        # The gate was just run against this very source, so the state it
        # preserved is the freshest evidence the folder has: the report shows
        # the pending non-PASS verdict with that failing command attached, and
        # must never call it stale and invite deleting the derived state.
        self.assertIn("Folder auto-fix: FAILED (fresh)", report)
        self.assertNotIn("Folder auto-fix: STALE", report)
        self.assertNotIn("source tree changed", report)
        self.assertIn("- FAIL (3) t001:", report)

    def test_the_settling_gate_reuses_a_pass_proven_against_this_source(self):
        log = self.gate_log()
        task_path = self.write_task(1, status="DONE", scope=("src/",),
                                    verify=self.recording_gate(log))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.",
            recommendation="Write the repaired value.")
        # The final round reports FIXED for a repair that is already on disk
        # from the repair round, so it writes nothing itself and the tree that
        # settles is exactly the tree the repair round's own gate proved.
        confirmed = auto_fix.ReviewFinding(
            finding.task_id, finding.path, finding.summary, finding.evidence,
            kind=finding.kind, recommendation=finding.recommendation,
            transition="still_present",
            prior_fingerprint=auto_fix.finding_fingerprint(finding),
            transition_evidence="The repair round's edit is the repair.")
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FIXED", (confirmed,))), False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "worker repair\n"})])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 0)

        # The initial folder gate and the repair's own closeout gate are the
        # only executions: the second round's review gate and the settling gate
        # both reuse that authoritative PASS instead of a third and fourth run.
        self.assertEqual(self.gate_runs(log), ["old", "worker repair"])
        self.assertEqual(out.getvalue().count("reused authoritative PASS"), 2)
        command = parse_task_file(task_path).verify
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNotNone(state.self_fixed_unreviewed)
        brief = next(item for item in state.repair_briefs
                     if item.task_id == "t001")
        self.assertIn(
            f"- PASS (reused authoritative PASS) t001: {command}", brief.brief)

    def test_fixed_rounds_walk_the_configured_adapter_list_positionally(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(
            3, "claude", "codex", "claude"))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.",
            recommendation="Write the repaired value.")
        fingerprint = auto_fix.finding_fingerprint(finding)
        repaired = auto_fix.ReviewFinding(
            finding.task_id, finding.path, finding.summary, finding.evidence,
            kind=finding.kind, recommendation=finding.recommendation,
            transition="still_present", prior_fingerprint=fingerprint,
            transition_evidence="The same blocker was repaired again in place.")

        def fix(text, expected_round, expected_remaining):
            def step(prompt):
                self.assertIn(
                    f"This is workflow plan step {expected_round * 2} of 6.", prompt)
                self.assertIn(
                    "Workflow steps remaining after this one: "
                    f"{expected_remaining * 2}.", prompt)
                self.assertIn("you may repair it directly", prompt)
                (self.execution_root() / "src" / "value.txt").write_text(
                    text, encoding="utf-8")
                return TaskResult(0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord(
                        "FIXED",
                        (finding if expected_round == 1 else repaired,))),
                    False, None)
            return step

        def final(prompt):
            self.assertIn("This is workflow plan step 6 of 6.", prompt)
            self.assertIn("Workflow steps remaining after this one: 0.", prompt)
            self.assertIn("This is the FINAL workflow plan step.", prompt)
            self.assertIn("No further review will occur after this one.", prompt)
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)

        reviewer = ScriptedAdapter([
            fix("first repair\n", 1, 2),
            fix("second repair\n", 2, 1),
            final,
        ])
        worker = ScriptedAdapter([])

        out = io.StringIO()
        with mock.patch("assent.preflight.get_adapter", return_value=reviewer), \
                contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 0)

        # Position, not identity history, selects each round: the repeated
        # third "claude" entry is used again rather than treated as consumed.
        self.assertEqual(self.review_session_agents(out.getvalue()),
                         ["claude", "codex", "claude"])
        self.assertEqual(len(worker.calls), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(state.reviewer_adapter, "claude")
        self.assertEqual(state.workflow_step_index, 5)
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertEqual(
            (self.execution_root() / "src" / "value.txt").read_text(
                encoding="utf-8"),
            "second repair\n")
        self.assertTrue(any(
            "review round 2 repaired its own finding" in subject
            for subject in self.subjects()))

    def test_a_fixed_round_writing_outside_the_named_task_scope_is_refused(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        (self.root / "other").mkdir()
        (self.root / "other" / "keep.txt").write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(2))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.")

        def repair_outside_scope(_prompt):
            (self.execution_root() / "other" / "keep.txt").write_text(
                "reviewer overreach\n", encoding="utf-8")
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FIXED", (finding,))), False, None)

        reviewer = ScriptedAdapter([repair_outside_scope])
        worker = ScriptedAdapter([])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 1)
        self.assertIn("wrote outside the declared scope", out.getvalue())
        self.assertIn("other/keep.txt", out.getvalue())
        # The verdict is not honored and no durable state records it, while the
        # exact edit is preserved for a human.
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())
        self.assertEqual(
            (self.execution_root() / "other" / "keep.txt").read_text(
                encoding="utf-8"),
            "reviewer overreach\n")
        self.assertEqual(len(reviewer.calls), 1)


class TestWorkflowAccountabilityUnit(GlobalContractsMixin, EngineTestCase):
    TASK_ROLES = (
        '\n[abilities.prepare]\nprompt = "Prepare the implementation."\n'
        'writes = true\ngate = false\n'
        '[abilities.implement]\nprompt = "Implement and verify."\n'
        'writes = true\ngate = true\n'
        '[agents.preparer]\nability = ["prepare"]\n'
        '[agents.implementer]\nability = ["prepare", "implement"]\n'
        '[workflow]\ntask = [{ role = "preparer" }, '
        '{ role = "implementer" }]\n')
    PLAN_ROLE = (
        '\n[abilities.implement_plan]\nprompt = "Implement the whole plan."\n'
        'writes = true\ngate = true\n'
        '[agents.plan_worker]\nability = ["implement_plan"]\n'
        'model = "lite"\n'
        '[workflow]\ntask = []\nplan = [{ role = "plan_worker" }]\n')

    @staticmethod
    def set_task_workflow(path, roles):
        rendered = ", ".join(
            f'{{ role = {json.dumps(role)} }}' for role in roles)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace('status = ', f'workflow = [{rendered}]\nstatus = ', 1),
            encoding="utf-8", newline="\n")

    def test_task_roles_run_as_separate_sessions(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()

        def prepare(_prompt):
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="progress",
                         summary="Preparation completed")
            return ok_result()

        adapter = ScriptedAdapter([prepare, self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 2)
        self.assertIn("scheduled role: preparer", adapter.calls[0][0])
        self.assertIn("scheduled role: implementer", adapter.calls[1][0])
        self.assertEqual(len([
            item for item in read_entries(journal_path_for(path))
            if item.get("by") != "scheduler"]), 2)
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_task_workflow_overrides_project_task_roles(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ("implementer",))
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()

        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("scheduled role: implementer", adapter.calls[0][0])
        self.assertNotIn("scheduled role: preparer", adapter.calls[0][0])

    def test_empty_task_workflow_uses_only_its_plan_unit(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ())
        cfg = self.build(extra_config=self.PLAN_ROLE)
        self.commit_all()

        adapter = ScriptedAdapter([ok_result()])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("Plan workflow step 1 of 1", adapter.calls[0][0])
        self.assertNotIn("Task workflow step", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_task_workflow_missing_role_refuses_before_any_session(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ("missing",))
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()
        adapter = ScriptedAdapter([])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = engine.run(cfg, once=True, adapter=adapter)

        self.assertEqual(result, 1)
        self.assertEqual(adapter.calls, [])
        self.assertIn("Task t001", out.getvalue())
        self.assertIn("missing agent role 'missing'", out.getvalue())

    def test_task_session_cannot_change_its_workflow(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ("implementer",))
        cfg = self.build(retry=0, extra_config=self.TASK_ROLES)
        self.commit_all()

        def tamper(_prompt):
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace('{ role = "implementer" }',
                             '{ role = "preparer" }'),
                encoding="utf-8", newline="\n")
            set_status(path, "DONE")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                event="done", summary="Attempted closeout")
            return ok_result()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                engine.run(cfg, once=True, adapter=ScriptedAdapter([tamper])), 0)

        self.assertIn("fields other than status were modified: workflow",
                      out.getvalue())
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_never_started_next_task_step_has_no_interrupt_prompt(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()

        def prepare(_prompt):
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="progress",
                         summary="Preparation completed")
            return ok_result()

        real_write = engine.write_workflow_state
        interrupted = False

        def stop_after_advance(tasks_dir, state):
            nonlocal interrupted
            real_write(tasks_dir, state)
            if state.step_index == 1 and not state.started and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with mock.patch.object(
                engine, "write_workflow_state", side_effect=stop_after_advance):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([prepare])), 130)

        resumed = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=resumed), 0)
        self.assertNotIn("previous adapter session was interrupted",
                         resumed.calls[0][0])

    def test_empty_task_workflow_runs_one_plan_unit(self):
        first = self.write_task(1, scope=("src/a.txt",))
        second = self.write_task(
            2, deps=("t001",), scope=("src/b.txt",))
        cfg = self.build(extra_config=self.PLAN_ROLE)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("old\n", encoding="utf-8")
        (self.root / "src" / "b.txt").write_text("old\n", encoding="utf-8")
        self.commit_all()

        def implement(prompt):
            self.assertIn(str(first), prompt)
            self.assertIn(str(second), prompt)
            self.assertIn("t002: TODO; deps: t001", prompt)
            self.assertNotIn("Cumulative checkpoint diff", prompt)
            for name in ("a.txt", "b.txt"):
                target = self.execution_root() / "src" / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("done\n", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([implement])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(
            [task.status for task in Plan.parse(cfg.tasks_dir).tasks],
            ["DONE", "DONE"])
        for path in (first, second):
            entry = read_entries(journal_path_for(path))[-1]
            self.assertEqual(entry["by"], "scheduler")
            self.assertIn("plan_worker", entry["summary"])
        self.assertTrue(self.subjects()[0].startswith(
            "auto(plan01): workflow plan step plan_worker"))
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_plan_gate_failure_retries_step_then_blocks_every_task(self):
        failing = 'python -c "raise SystemExit(3)"'
        first = self.write_task(1, verify=failing)
        second = self.write_task(2, deps=("t001",), verify=failing)
        cfg = self.build(retry=1, extra_config=self.PLAN_ROLE)
        self.commit_all()
        adapter = ScriptedAdapter([ok_result(), ok_result()])

        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(
            [task.status for task in Plan.parse(cfg.tasks_dir).tasks],
            ["BLOCKED", "BLOCKED"])
        for path in (first, second):
            entry = read_entries(journal_path_for(path))[-1]
            self.assertIn("plan_worker", entry["summary"])
            self.assertIn("exit code is non-zero", entry["detail"])
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())


class TestAntigravitySession(GlobalContractsMixin, EngineTestCase):
    def setUp(self):
        super().setUp()
        from assent.adapters import antigravity
        self.antigravity = antigravity
        self.catalog = antigravity.parse_models_catalog(
            (Path(__file__).resolve().parent / "fixtures"
             / "agy_models_1.1.5.txt").read_text(encoding="utf-8"))

    def adapter(self, cfg):
        from assent.adapters.antigravity import AntigravityAdapter
        return AntigravityAdapter(cfg, catalog=self.catalog)

    def patch_session(self, fake):
        patch = mock.patch.object(self.antigravity, "run_subprocess", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_legacy_instructions_example_does_not_break_the_resolved_identity(self):
        # The instructions file of an older project still shows only codex/claude. The
        # run-specific prompt, the closeout the agent writes, and the journal validator must
        # all still agree on antigravity, so the task passes on the first attempt.
        path = self.write_task(1, model="lite")
        (self.root / ".assent" / "instructions.md").write_text(
            "工作指示\n\n日誌 by 欄位範例:by = \"codex\" 或 \"claude\"\n",
            encoding="utf-8")
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter]\nname = "antigravity"\n', encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        commands: list[list[str]] = []
        prompts: list[str] = []

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None,
                 input_text=None):
            commands.append(command)
            prompts.append(input_text)
            (Path(cwd) / "src").mkdir(exist_ok=True)
            (Path(cwd) / "src" / "done.py").write_text("ok", encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="antigravity",
                         requested_model=command[command.index("--model") + 1],
                         requested_effort=command[command.index("--effort") + 1],
                         event="done", summary="完成")
            return 0, "done\n", False

        self.patch_session(fake)
        adapter = self.adapter(cfg)
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(commands), 1)      # first attempt accepted, no retry loop
        prompt = prompts[0]
        self.assertIn('by = "antigravity"', prompt)
        self.assertIn('requested_model = "gemini-3.5-flash"', prompt)
        self.assertIn('requested_effort = "medium"', prompt)
        self.assertIn("authoritative for this run's journal entry", prompt)
        self.assertEqual(parse_task_file(path).status, "DONE")

        from assent.plan import read_entries
        done = next(e for e in read_entries(journal_path_for(path))
                    if e["by"] == "antigravity")
        # the journal identity is the actual flag pair, not the abstract tier
        self.assertEqual(done["requested_model"], "gemini-3.5-flash")
        self.assertEqual(done["requested_effort"], "medium")
        self.assertTrue(any(s.startswith("auto(plan01/t001): ")
                            for s in self.subjects()))

    def test_adapter_classification_reaches_the_reason_and_the_journal(self):
        path = self.write_task(1, model="lite")
        (self.root / ".assent" / "assent.toml").write_text(
            '[run]\nretry_per_task = 0\n[adapter]\nname = "antigravity"\n',
            encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        self.patch_session(lambda *args, **kwargs: (
            1, "Error: permission denied for tool write_to_file", False))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True,
                                        adapter=self.adapter(cfg)), 0)

        self.assertIn("classified by the adapter as permission", out.getvalue())
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        failure = next(e for e in entries if e["event"] == "adapter_exit")
        self.assertEqual(failure["failure_kind"], "permission")
        self.assertEqual(failure["exit_code"], 1)
        self.assertEqual(failure["agent"], "antigravity")
        # a classification never turns a non-zero exit into an accepted task
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_quota_interrupt_then_resume_keeps_identity_everywhere(self):
        """A quota round and its resume must resolve to the exact same requested_model /
        requested_effort in the prompt, the CLI command, the terminal output, and both the
        scheduler's quota journal entry and the execution AI's own done entry."""
        path = self.write_task(1, model="prime", effort="slight")
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter]\nname = "antigravity"\n', encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        sleeps: list[float] = []
        calls: list[list[str]] = []
        prompts: list[str] = []

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None,
                 input_text=None):
            calls.append(command)
            prompts.append(input_text)
            if len(calls) == 1:
                return (1, "Error: Resource has been exhausted (e.g. check quota).", False)
            (Path(cwd) / "src").mkdir(exist_ok=True)
            (Path(cwd) / "src" / "done.py").write_text("ok", encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="antigravity",
                         requested_model=command[command.index("--model") + 1],
                         requested_effort=command[command.index("--effort") + 1],
                         event="done", summary="完成 (resumed)")
            return (0, "done\n", False)

        self.patch_session(fake)
        adapter = self.adapter(cfg)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=adapter,
                sleep=sleeps.append, now=lambda: t0), 0)

        self.assertEqual(len(calls), 2)     # quota round, then one resumed round
        terminal = out.getvalue()
        for command in calls:
            self.assertEqual(command[command.index("--model") + 1], "gemini-3.1-pro")
            self.assertEqual(command[command.index("--effort") + 1], "low")
        self.assertIn("gemini-3.1-pro", terminal)

        resume_prompt = prompts[1]
        self.assertIn("resume", resume_prompt.lower())
        self.assertIn('requested_model = "gemini-3.1-pro"', resume_prompt)
        self.assertIn('requested_effort = "low"', resume_prompt)

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota_entry = next(e for e in entries if e["event"] == "quota")
        done_entry = next(e for e in entries if e["event"] == "done")
        self.assertEqual(quota_entry["requested_model"], "gemini-3.1-pro")
        self.assertEqual(quota_entry["requested_effort"], "low")
        self.assertEqual(done_entry["requested_model"], "gemini-3.1-pro")
        self.assertEqual(done_entry["requested_effort"], "low")
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestAdapterProcessOutcomes(GlobalContractsMixin, EngineTestCase):
    def test_nonzero_done_retries_without_done_checkpoint_then_succeeds(self):
        path = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        def failed_done(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "kept.py").write_text("work", encoding="utf-8")
            set_status(path, "DONE")
            output = ("transport failed token=TOPSECRET " + "x" * 400
                      + " useful-tail")
            return TaskResult(exit_code=7, output=output,
                              quota_exhausted=False, reset_at=None)

        def successful_retry(prompt):
            self.assertFalse(any(
                subject.startswith("auto(plan01/t001): ")
                for subject in self.subjects()))
            self.assertIn("exit code 7", prompt)
            self.assertIn("useful-tail", prompt)
            self.assertNotIn("TOPSECRET", prompt)
            return ok_result()

        adapter = ScriptedAdapter([failed_done, successful_retry])
        with mock.patch.object(engine, "_run_verify", return_value=0) as verify:
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=adapter), 0)
        verify.assert_called_once()
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertIn("src/kept.py", self._git_execution("ls-files"))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        failed = next(e for e in entries if e["event"] == "adapter_exit")
        self.assertEqual(failed["exit_code"], 7)
        self.assertFalse(failed["stalled"])
        self.assertEqual(failed["agent"], "claude")
        self.assertEqual(failed["requested_model"], "lite")
        self.assertEqual(failed["requested_effort"], "medium")
        self.assertNotIn("TOPSECRET", failed["summary"])
        self.assertLess(len(failed["summary"]), 400)

    def test_nonzero_todo_exhausts_retries_and_keeps_work(self):
        path = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        def first(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(exit_code=2, output="first transport failure",
                              quota_exhausted=False, reset_at=None)

        adapter = ScriptedAdapter([
            first,
            TaskResult(exit_code=4, output="second transport failure",
                       quota_exhausted=False, reset_at=None),
        ])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertIn("exit code 2", adapter.calls[1][0])
        self.assertIn("src/partial.py", self._git_execution("ls-files"))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        failures = [e for e in entries if e["event"] == "adapter_exit"]
        self.assertEqual([e["exit_code"] for e in failures], [2, 4])
        blocked = next(e for e in entries if e["event"] == "blocked")
        self.assertIn("exit code 4", blocked["summary"])

    def test_nonzero_self_blocked_retries_then_scheduler_blocks(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def blocked(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text(
                "preserved", encoding="utf-8")
            set_status(path, "BLOCKED")
            return TaskResult(exit_code=9, output="dependency unavailable",
                              quota_exhausted=False, reset_at=None)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=ScriptedAdapter([blocked]))
        self.assertEqual(rc, 0)
        self.assertIn("exit code 9", out.getvalue())
        self.assertFalse(any(
            subject == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for subject in self.subjects()))

        from assent.plan import read_entries
        failure = next(e for e in read_entries(journal_path_for(path))
                       if e["event"] == "adapter_exit")
        self.assertEqual(failure["exit_code"], 9)
        scheduler_blocked = next(e for e in read_entries(journal_path_for(path))
                                 if e["by"] == "scheduler"
                                 and e["event"] == "blocked")
        self.assertIn("exit code 9", scheduler_blocked["summary"])

    def test_nonzero_self_blocked_with_scope_violation_fails_closed(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def unsafe_blocked(prompt):
            (self.execution_root() / "rogue.py").write_text(
                "unsafe", encoding="utf-8")
            path.write_text(
                task_text(status="BLOCKED", scope=("src/", "rogue.py"),
                          verify="echo unsafe"),
                encoding="utf-8", newline="\n")
            return TaskResult(exit_code=8, output="adapter failed",
                              quota_exhausted=False, reset_at=None)

        self.run_quiet(cfg, once=True,
                       adapter=ScriptedAdapter([unsafe_blocked]))
        self.assertFalse(any(
            subject == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for subject in self.subjects()))
        from assent.plan import read_entries
        blocked = next(e for e in read_entries(journal_path_for(path))
                       if e["event"] == "blocked")
        self.assertIn("exit code 8", blocked["summary"])
        self.assertIn("fields other than status", blocked["summary"])
        self.assertIn("outside scope", blocked["summary"])

    def test_watchdog_stall_has_distinct_non_quota_event(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()
        stalled = TaskResult(exit_code=1, output="rate limit exceeded",
                             quota_exhausted=False, reset_at=None, stalled=True)
        self.run_quiet(cfg, once=True, adapter=ScriptedAdapter([stalled]))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertIn("adapter_stall", [e["event"] for e in entries])
        self.assertNotIn("quota", [e["event"] for e in entries])


class TestBillingAbort(GlobalContractsMixin, EngineTestCase):
    """A billing/insufficient-balance failure aborts the whole run without a retry.

    Dispatch is purely on failure_kind="billing" (never an adapter name), so this uses a
    plain ScriptedAdapter result, exactly how a fourth adapter would opt in."""

    def billing_step(self, path):
        def step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(
                exit_code=1, output="Credit balance is too low",
                quota_exhausted=False, reset_at=None, failure_kind="billing")
        return step

    def test_billing_consumes_no_retry_and_aborts_before_next_task(self):
        p1 = self.write_task(1)
        p2 = self.write_task(2)
        cfg = self.build(retry=2)          # retries available, but none may be spent
        self.commit_all()

        # only one step: a second call (a retry, or advancing to t002) would raise
        adapter = ScriptedAdapter([self.billing_step(p1)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, adapter=adapter)

        self.assertEqual(rc, 1)
        self.assertEqual(len(adapter.calls), 1)             # no retry, no advance to t002
        text = out.getvalue()
        self.assertIn("billing/balance", text)
        self.assertIn("Top up the account", text)

        # the failing task stays resumable (WIP), the next task is untouched (TODO)
        self.assertEqual(parse_task_file(p1).status, "WIP")
        self.assertEqual(parse_task_file(p2).status, "TODO")

        # progress kept in a wip checkpoint; no BLOCKED checkpoint was written
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ") for s in subjects))
        self.assertFalse(any("BLOCKED" in s for s in subjects))
        self.assertIn("src/partial.py", self._git_execution("ls-files"))

        # a distinct billing journal entry, separate from a normal acceptance failure
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(p1))
        billing = next(e for e in entries
                       if e["by"] == "scheduler" and e["event"] == "billing")
        self.assertIn("top-up", billing["summary"].lower())
        self.assertEqual(billing["agent"], "claude")
        self.assertEqual(billing["requested_model"], "lite")
        self.assertNotIn("blocked", [e["event"] for e in entries])

    def test_billing_task_resumes_cleanly_on_a_later_run(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([self.billing_step(path)])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")

        # after a top-up, the same task is picked up again and resumes with a continue prompt
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestQuotaAndResume(GlobalContractsMixin, EngineTestCase):
    def rotation_config(self):
        cfg = self.build()
        cfg.adapter_names = ("claude", "codex")
        return cfg

    def test_quota_waits_then_resumes_same_task(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        reset = t0 + timedelta(minutes=5)
        sleeps: list[float] = []

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("p", encoding="utf-8")
            # The adapter can write DONE before the process result is classified as quota;
            # the scheduler must put the task back into the resumable state first.
            set_status(path, "DONE")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=reset)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            return self.ai_done(path)(prompt)

        adapter = ScriptedAdapter([quota_step, resumed])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), (5 + 2) * 60, delta=1)  # +2 minute buffer
        self.assertIn("resume", adapter.calls[1][0])
        self.assertIn("Waiting for quota reset before resuming", out.getvalue())
        # one zero-token capability preflight before the run, then one per attempt
        self.assertEqual(adapter.resolve_calls, ["lite", "lite", "lite"])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in subjects))
        self.assertEqual(
            len([s for s in subjects if s.startswith("auto(plan01/t001): ")]), 1)
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "lite")
        self.assertEqual(quota["requested_effort"], "medium")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, waiting for quota reset before resuming")
        self.assertNotIn("session", [e["event"] for e in entries])

    def test_unknown_quota_wait_names_poll_and_preserves_resume_progress(self):
        path = self.write_task(1)
        cfg = self.build()
        cfg.quota_poll_minutes = 7
        self.commit_all()

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            set_status(path, "DONE")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            self.assertIn("resume", prompt.lower())
            return self.ai_done(path)(prompt)

        adapter = ScriptedAdapter([quota_step, resumed])
        sleeps: list[float] = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append)

        self.assertEqual(rc, 0)
        self.assertEqual(sum(sleeps), 7 * 60)
        self.assertEqual(parse_task_file(path).status, "DONE")
        terminal = out.getvalue()
        self.assertIn(
            "Waiting for quota poll (every 7 minutes) before resuming", terminal)
        self.assertIn("Quota poll (every 7 minutes)", terminal)
        self.assertNotIn("reset", terminal.lower())
        self.assertIn("src/partial.py", self._git_execution("ls-files"))
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in self.subjects()))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, waiting for quota poll "
            "(every 7 minutes) before resuming")
        self.assertNotIn("reset", quota["summary"].lower())

    def test_checkpoint_resume_keeps_same_adapter_without_wait_rotation_or_retry(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        cfg.adapter_names = ("claude", "codex")
        self.commit_all()
        control = TaskResult(
            exit_code=1, output=CHECKPOINT_RESUME_RECORD + "\n",
            quota_exhausted=False, reset_at=None, checkpoint_resume=True)

        def checkpoint_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            set_status(path, "DONE")
            return control

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            return self.ai_done(path)(prompt)

        claude = ScriptedAdapter(
            [checkpoint_step, resumed], resolved_model="claude-lite")
        codex = ScriptedAdapter([], resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            with mock.patch("assent.engine.get_adapter", return_value=codex):
                result = engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append)

        self.assertEqual(result, 0)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(codex.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(claude.calls[0][1:], claude.calls[1][1:])
        self.assertIn("resume", claude.calls[1][0].lower())

        terminal = out.getvalue()
        self.assertIn("Checkpoint-resume control received", terminal)
        self.assertIn("same adapter command", terminal)
        self.assertNotIn("Waiting for quota reset", terminal)
        self.assertNotIn("Switching adapter", terminal)

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        checkpoint = next(entry for entry in entries
                          if entry["event"] == "checkpoint_resume")
        self.assertEqual(checkpoint["agent"], "claude")
        self.assertEqual(checkpoint["requested_model"], "claude-lite")
        self.assertEqual(checkpoint["requested_effort"], "medium")
        self.assertIn(CHECKPOINT_RESUME_RECORD, checkpoint["detail"])
        self.assertNotIn("quota", [entry["event"] for entry in entries])
        self.assertTrue(any(subject.startswith("wip(plan01/t001): ")
                            for subject in self.subjects()))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_quota_preserves_an_explicit_blocked_result(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def blocked_quota(prompt):
            set_status(path, "BLOCKED")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "BLOCKED")
            return ok_result()

        self.assertEqual(
            engine.run(cfg, once=True, sleep=lambda _: None,
                       adapter=ScriptedAdapter([blocked_quota, resumed])), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertFalse(any(s.startswith("auto(plan01/t001): ")
                             for s in self.subjects()))

    def test_quota_rotates_to_next_adapter_and_records_each_identity(self):
        path = self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(
                exit_code=1, output="", quota_exhausted=True, reset_at=None)

        claude = ScriptedAdapter([quota_step], resolved_model="claude-lite")
        codex = ScriptedAdapter(
            [self.ai_done(
                path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            with mock.patch("assent.engine.get_adapter", return_value=codex):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append), 0)

        self.assertEqual(sleeps, [])
        self.assertIn("resume", codex.calls[0][0])
        terminal = out.getvalue()
        self.assertIn("Switching adapter claude -> codex immediately", terminal)
        self.assertNotIn("waiting for reset", terminal.lower())
        self.assertNotIn("rotation poll", terminal.lower())
        self.assertTrue(any(
            subject.startswith("wip(plan01/t001): ")
            for subject in self.subjects()))
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(entry for entry in entries if entry["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "claude-lite")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, switching immediately to adapter codex")
        self.assertNotIn("wait", quota["summary"].lower())
        done = next(entry for entry in entries if entry["by"] == "codex")
        self.assertEqual(done["requested_model"], "codex-lite")

    def test_rotation_resolves_each_adapter_effort_and_names_it_in_the_line(self):
        # Each adapter resolves its own built-in lite default independently
        # (claude normal -> medium, codex slight -> low), and the opening line says
        # which one is running.
        path = self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()

        def quota_step(prompt):
            return TaskResult(
                exit_code=1, output="", quota_exhausted=True, reset_at=None)

        claude = ScriptedAdapter([quota_step], resolved_model="claude-lite")
        codex = ScriptedAdapter(
            [self.ai_done(path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=lambda _: None), 0)

        self.assertEqual(claude.calls[0][2], "medium")
        self.assertEqual(codex.calls[0][2], "low")
        self.assertEqual(
            [line for line in out.getvalue().splitlines() if "Session:" in line],
            ["  Session: claude | lite->claude-lite | normal->medium",
             "  Session: codex | lite->codex-lite | slight->low"])

    def test_complete_quota_rotation_waits_then_continues_from_next_adapter(self):
        path = self.write_task(1)
        cfg = self.rotation_config()
        cfg.rotation_poll_minutes = 2
        self.commit_all()
        quota = TaskResult(
            exit_code=1, output="", quota_exhausted=True, reset_at=None)
        claude = ScriptedAdapter(
            [quota, self.ai_done(path)], resolved_model="claude-lite")
        codex = ScriptedAdapter([quota], resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                result = engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append)

        self.assertEqual(result, 0)
        self.assertEqual(sum(sleeps), 2 * 60)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(len(codex.calls), 1)
        self.assertIn("Every adapter in the rotation is quota-exhausted",
                      out.getvalue())
        self.assertIn("continuing with claude", out.getvalue())
        self.assertEqual(out.getvalue().count(
            "Every adapter in the rotation is quota-exhausted"), 1)

        from assent.plan import read_entries
        quotas = [entry for entry in read_entries(journal_path_for(path))
                  if entry["event"] == "quota"]
        self.assertEqual(len(quotas), 2)
        self.assertEqual(
            [(entry["agent"], entry["requested_model"])
             for entry in quotas],
            [("claude", "claude-lite"), ("codex", "codex-lite")])
        self.assertEqual(
            quotas[0]["summary"],
            "Quota exhausted; progress kept, switching immediately to adapter codex")
        self.assertEqual(
            quotas[1]["summary"],
            "Quota exhausted; progress kept, every adapter in the rotation is "
            "quota-exhausted; waiting for rotation poll before continuing with claude")

    def test_all_rotation_adapters_are_preflighted_before_worktree_creation(self):
        self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()
        claude = ScriptedAdapter([ok_result()])
        codex = ScriptedAdapter([])
        codex.preflight = mock.Mock(return_value=["unsupported invocation"])
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                result = engine.run(cfg, once=True, adapter=claude)

        self.assertEqual(result, 1)
        self.assertEqual(claude.calls, [])
        self.assertEqual(codex.calls, [])
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())
        codex.preflight.assert_called_once()
        self.assertIn("codex capability preflight: FAIL", out.getvalue())

    def test_wip_task_resumed_on_startup(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestInterruptedTaskResume(GlobalContractsMixin, EngineTestCase):
    def test_keyboard_interrupt_marks_unverified_done_wip_then_resumes(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def interrupted(prompt):
            set_status(path, "DONE")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([interrupted])), 130)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        interrupt = next(e for e in entries
                         if e["by"] == "scheduler"
                         and e["event"] == "interrupt"
                         and "User interrupt" in e["summary"])
        self.assertEqual(interrupt["agent"], "claude")
        self.assertEqual(interrupt["requested_model"], "lite")
        self.assertEqual(interrupt["requested_effort"], "medium")

        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_interrupt_during_post_auto_report_keeps_done_without_duplicate_auto(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        refreshes = 0

        def interrupt_first_report(cfg):
            nonlocal refreshes
            refreshes += 1
            if refreshes == 1:
                raise KeyboardInterrupt

        with mock.patch.object(engine, "try_write_report",
                               side_effect=interrupt_first_report):
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=ScriptedAdapter([
                    self.ai_done(path, {"src/done.py": "done"})])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        autos = [s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]
        self.assertEqual(len(autos), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        # A later run sees the terminal task as already closed and cannot synthesize another
        # auto marker from the report-refresh interruption.
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_interrupt_after_dirty_terminal_auto_commit_keeps_done(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        real_commit_if_dirty = engine.gitops.commit_if_dirty

        def commit_then_interrupt(root, message, excludes=()):
            committed = real_commit_if_dirty(root, message, excludes)
            if message.startswith("auto(plan01/t001): "):
                raise KeyboardInterrupt
            return committed

        with mock.patch.object(engine.gitops, "commit_if_dirty",
                               side_effect=commit_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=ScriptedAdapter([
                    self.ai_done(path, {"src/done.py": "done"})])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(entry["event"] == "interrupt" for entry in entries))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_interrupt_after_resumed_empty_terminal_auto_commit_keeps_done(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        real_commit_empty = engine.gitops.commit_empty

        def empty_commit_then_interrupt(root, message):
            real_commit_empty(root, message)
            if message.startswith("auto(plan01/t001): "):
                raise KeyboardInterrupt

        with mock.patch.object(engine.gitops, "commit_empty",
                               side_effect=empty_commit_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([
                    self.ai_done(path)])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(entry["event"] == "interrupt" for entry in entries))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_matching_auto_commit_before_terminal_closeout_cannot_recover_done(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        expected = "auto(plan01/t001): task"

        def interrupted(prompt):
            root = self.execution_root()
            (root / "src" / "before_closeout.py").parent.mkdir(exist_ok=True)
            (root / "src" / "before_closeout.py").write_text(
                "work", encoding="utf-8")
            # This terminal-looking commit belongs to the adapter phase, before the scheduler
            # has passed _evaluate and armed its closeout witness.
            gitops.commit_empty(root, expected)
            set_status(path, "DONE")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([interrupted])), 130)

        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(entry["event"] == "interrupt" for entry in entries))
        self.assertIn(expected, self.subjects())
        self.assertTrue(any(
            subject.startswith("wip(plan01/t001): ")
            for subject in self.subjects()))

    def test_assent_error_marks_current_task_wip_and_keeps_exit_code(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def failed(prompt):
            raise AssentError("連線中斷")

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([failed])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(e["event"] == "interrupt"
                            and "infrastructure error" in e["summary"]
                            for e in entries))

    def test_os_error_marks_current_task_wip_as_infrastructure_failure(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def failed(prompt):
            raise OSError("adapter executable unavailable")

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([failed])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        interrupt = next(e for e in read_entries(journal_path_for(path))
                         if e["event"] == "interrupt")
        self.assertIn("infrastructure error", interrupt["summary"])

    def test_interrupted_self_blocked_task_is_not_overwritten(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def blocked_then_interrupted(prompt):
            set_status(path, "BLOCKED")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True,
            adapter=ScriptedAdapter([blocked_then_interrupted])), 130)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_quota_wait_interrupt_marks_task_wip(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        quota = TaskResult(exit_code=1, output="", quota_exhausted=True,
                           reset_at=None)

        def interrupt_sleep(seconds):
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([quota]),
            sleep=interrupt_sleep), 130)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        events = [e["event"] for e in read_entries(journal_path_for(path))]
        self.assertIn("quota", events)
        self.assertIn("interrupt", events)

    def test_stop_wake_during_quota_wait_reaches_the_interrupt_cleanup(self):
        """The wake is only a wake: it releases the sleeping main thread so the
        stdin watcher's already-pending KeyboardInterrupt is delivered, and the
        ordinary interrupt cleanup then runs unchanged."""
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        quota = TaskResult(exit_code=1, output="", quota_exhausted=True,
                           reset_at=None)
        parked = threading.Event()

        def stop_the_run() -> None:
            """Exactly what the stdin stop watcher does on EOF, in that order:
            mark the interrupt, then release the wait it is stuck behind."""
            parked.wait(30)
            _thread.interrupt_main()
            wake_stop_waiters()

        waker = threading.Thread(target=stop_the_run, daemon=True)
        self.addCleanup(waker.join, 30)
        self.addCleanup(clear_stop_wake)

        def sleep(seconds):
            # The production wait, entered on a segment of the full length: a
            # pending KeyboardInterrupt alone would sit here for 60 seconds.
            self.assertEqual(seconds, engine._COUNTDOWN_SEGMENT)
            parked.set()
            interruptible_sleep(seconds)

        waker.start()
        started = time.monotonic()
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([quota]),
            sleep=sleep), 130)
        self.assertLess(time.monotonic() - started, engine._COUNTDOWN_SEGMENT)

        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        events = [e["event"] for e in read_entries(journal_path_for(path))]
        self.assertIn("quota", events)
        self.assertIn("interrupt", events)

    def test_idle_interrupt_does_not_mark_any_task(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        real_parse = engine.Plan.parse
        calls = 0

        def parse_then_interrupt(plan_dir):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return real_parse(plan_dir)

        with mock.patch.object(engine.Plan, "parse",
                               side_effect=parse_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([])), 130)
        self.assertEqual(parse_task_file(path).status, "TODO")
        self.assertFalse(journal_path_for(path).exists())


class TestQuotaMath(GlobalContractsMixin, EngineTestCase):
    def test_quota_wait_seconds(self):
        cfg = self.build()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        future = t0 + timedelta(minutes=10)
        self.assertAlmostEqual(
            engine._quota_wait_seconds(cfg, future, lambda: t0), 12 * 60, delta=1)
        past = t0 - timedelta(hours=1)
        self.assertEqual(engine._quota_wait_seconds(cfg, past, lambda: t0), 0.0)
        self.assertEqual(engine._quota_wait_seconds(cfg, None, lambda: t0),
                         cfg.quota_poll_minutes * 60)

    def test_countdown_non_tty_single_line_in_bounded_segments(self):
        stream = io.StringIO()  # isatty() False
        sleeps: list[float] = []
        engine._countdown(150, "Quota reset", sleeps.append, stream=stream)
        # One message, but never one long sleep: the total is unchanged and no
        # single segment exceeds the constant.
        self.assertEqual(sum(sleeps), 150)
        self.assertLessEqual(max(sleeps), engine._COUNTDOWN_SEGMENT)
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertNotIn("\r", stream.getvalue())

    def test_countdown_non_tty_stop_lands_within_one_segment(self):
        """A stop request reaches a multi-hour quota wait promptly.

        The stdin stop channel calls _thread.interrupt_main(); on POSIX that
        only makes the exception pending until bytecode next runs, which is
        the end of a segment. The injected sleep stands in for that delivery.
        """
        stream = io.StringIO()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            engine._countdown(10405, "Quota reset", sleep, segment=0.5,
                              stream=stream)
        self.assertEqual(sleeps, [0.5])

    def test_countdown_stops_counting_down_once_a_stop_is_requested(self):
        """A woken segment must not be followed by the rest of a multi-hour
        wait; the pending KeyboardInterrupt lands at the next bytecode."""
        self.addCleanup(clear_stop_wake)
        stream = io.StringIO()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            wake_stop_waiters()   # what the stdin watcher does mid-wait

        engine._countdown(10405, "Quota reset", sleep, stream=stream)
        self.assertEqual(sleeps, [engine._COUNTDOWN_SEGMENT])

    def test_stale_stop_request_does_not_shorten_a_later_countdown(self):
        """`run` is also a library and test entry point, so one stop request
        must not make every later countdown return immediately."""
        self.addCleanup(clear_stop_wake)
        wake_stop_waiters()
        stream = io.StringIO()
        sleeps: list[float] = []
        engine._countdown(150, "Quota reset", sleeps.append, stream=stream)
        self.assertEqual(sum(sleeps), 150)

    def test_countdown_tty_updates_in_place(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        stream = Tty()
        sleeps: list[float] = []
        engine._countdown(3, "Quota reset", sleeps.append, stream=stream)
        out = stream.getvalue()
        self.assertEqual(sum(sleeps), 3)
        self.assertGreaterEqual(out.count("\r"), 3)
        self.assertNotIn("\n", out)


if __name__ == "__main__":
    unittest.main()
