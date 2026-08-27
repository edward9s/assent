"""The workflow engine is one finite role/action interpreter."""
import contextlib
import io
import os
import subprocess
import unittest
from unittest import mock

from assent import engine, gitops, ignored_dirs
from assent.adapters import TaskResult
from assent.plan import (parse_task_file, read_entries,
                         plan_workflow_requires_human,
                         read_selection_workflow_state, read_workflow_state,
                         WorkflowState, write_workflow_state)
from assent.verification_common import FullVerifyEvidence
from tests.engine_support import EngineTestCase, ScriptedAdapter, _NEEDS_OK_TXT


def result(output="session complete"):
    return TaskResult(0, output, False, None)


WORKFLOW = """
[abilities.implement]
prompt = "Implement the task with the smallest coherent design."
writes = true
[abilities.repair]
prompt = "Inspect the failed action and repair the candidate simply."
writes = true
[roles.implementer]
ability = ["implement"]
model = "lite"
[roles.repairer]
ability = ["repair"]
model = "lite"
[workflow]
task = [{ role = "implementer" }, { action = "focused_test" },
        { role = "repairer" }, { action = "focused_test" }]
plan = []
integration = [{ action = "full_verify" }, { role = "repairer" },
               { action = "full_verify" }]
"""

ACTION_ONLY_WORKFLOW = """
[workflow]
task = [{ action = "focused_test" }]
plan = []
integration = []
"""

PLAN_ACTION_WORKFLOW = """
[workflow]
task = [{ action = "focused_test" }]
plan = [{ action = "focused_sweep" }]
integration = []
"""

SEPARATE_REVIEW_WORKFLOW = """
[abilities.review]
prompt = "Inspect the failed action without editing files."
writes = false
[abilities.fix]
prompt = "Repair the review evidence."
writes = true
[roles.reviewer]
ability = ["review"]
model = "lite"
[roles.fixer]
ability = ["fix"]
model = "lite"
[workflow]
task = [{ action = "focused_test" }, { role = "reviewer" },
        { role = "fixer" }, { action = "focused_test" }]
plan = []
integration = []
"""

ROTATING_WORKFLOW = """
[abilities.work]
prompt = "Complete the task."
writes = true
[roles.worker]
ability = ["work"]
model = "lite"
[workflow]
task = [{ role = "worker", adapter = ["claude", "codex"] },
        { action = "focused_test" }]
plan = []
integration = []
"""


class TestLinearEngine(EngineTestCase):
    def run_with_contracts(self, cfg, adapter, **kwargs):
        with mock.patch.object(engine.contracts, "require_contracts"):
            return self.run_quiet(cfg, adapter=adapter, **kwargs)

    def test_role_edits_source_and_scheduler_completes_task(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()

        def implement(_prompt):
            target = self.execution_root() / "src" / "ok.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("ok\n", encoding="utf-8")
            return result("created the required source file")

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([implement])), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue((self.execution_root() / "src" / "ok.txt").is_file())

    def test_run_processes_every_task(self):
        first = self.write_task(1)
        second = self.write_task(2, deps=("t001",))
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result(), result()])), 0)
        self.assertEqual(parse_task_file(first).status, "DONE")
        self.assertEqual(parse_task_file(second).status, "DONE")

    def test_ai_session_names_the_task_and_resolved_invocation(self):
        self.write_task(1, title="Visible task")
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        output = io.StringIO()
        adapter = ScriptedAdapter([result()])

        with mock.patch.object(engine.contracts, "require_contracts"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(engine.run(
                cfg, adapter=adapter), 0)

        text = output.getvalue()
        self.assertIn("Task t001: Visible task", text)
        self.assertIn("Session: claude | lite->sonnet/medium", text)
        self.assertIn("Configured AI session: 1 of 2", adapter.calls[0][0])
        self.assertIn("Do not narrate plans, internal deliberation, or rhetorical "
                      "questions.", adapter.calls[0][0])

    def test_action_only_task_workflow_starts_no_ai_session(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=ACTION_ONLY_WORKFLOW)
        self.commit_all()
        adapter = ScriptedAdapter([])

        self.assertEqual(self.run_with_contracts(cfg, adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(adapter.calls, [])

    def test_failed_action_advances_to_repair_with_evidence(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()

        def inspect(_prompt):
            return result("implementation inspected; file still missing")

        def repair(prompt):
            self.assertIn("FOCUSED TEST EVIDENCE", prompt)
            self.assertIn("implementation inspected", prompt)
            target = self.execution_root() / "src" / "ok.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixed\n", encoding="utf-8")
            return result("repaired the failed focused action")

        adapter = ScriptedAdapter([inspect, repair])
        self.assertEqual(self.run_with_contracts(cfg, adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(adapter.calls), 2)
        self.assertIn("Configured AI session: 1 of 2", adapter.calls[0][0])
        self.assertIn("Configured AI session: 2 of 2", adapter.calls[1][0])

    def test_separate_reviewer_evidence_reaches_the_fixer(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(extra_config=SEPARATE_REVIEW_WORKFLOW)
        self.commit_all()

        def review(prompt):
            self.assertIn("FOCUSED TEST EVIDENCE", prompt)
            return result("Reviewer found src/ok.txt missing.")

        def fix(prompt):
            self.assertIn("reviewer:\nReviewer found src/ok.txt missing.", prompt)
            target = self.execution_root() / "src" / "ok.txt"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixed\n", encoding="utf-8")
            return result("fixed the reviewed problem")

        adapter = ScriptedAdapter([review, fix])
        self.assertEqual(self.run_with_contracts(cfg, adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(adapter.calls), 2)

    def test_billing_failure_is_journaled_and_remains_resumable(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        billing = TaskResult(
            1, "credit balance is too low", False, None,
            failure_kind="billing")

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([billing])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")
        entries = read_entries(parse_task_file(path).journal_path)
        self.assertEqual(entries[-1]["event"], "billing")
        self.assertIn("manual top-up", entries[-1]["summary"])

    def test_authentication_failure_uses_the_next_declared_adapter(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=ROTATING_WORKFLOW)
        self.commit_all()
        unauthenticated = TaskResult(
            1, "authentication required", False, None,
            failure_kind="authentication")
        claude = ScriptedAdapter([unauthenticated])
        codex = ScriptedAdapter([result("completed with fallback")])

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            code = self.run_with_contracts(cfg, claude)

        self.assertEqual(code, 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(codex.calls), 1)

    def test_all_declared_authentication_failures_stop_without_waiting(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=ROTATING_WORKFLOW)
        self.commit_all()
        unauthenticated = TaskResult(
            1, "authentication required", False, None,
            failure_kind="authentication")
        claude = ScriptedAdapter([unauthenticated])
        codex = ScriptedAdapter([unauthenticated])

        def forbidden_sleep(_seconds):
            raise AssertionError("authentication failure must not wait")

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            code = self.run_with_contracts(
                cfg, claude, sleep=forbidden_sleep)

        self.assertEqual(code, 1)
        self.assertEqual(parse_task_file(path).status, "WIP")

    def test_quota_wait_resumes_the_same_role_without_losing_progress(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        cfg.quota_poll_minutes = 1
        self.commit_all()
        quota = TaskResult(
            1, "quota exhausted", True, None, failure_kind="quota")
        adapter = ScriptedAdapter([quota, result("resumed")])
        waits = []

        self.assertEqual(self.run_with_contracts(
            cfg, adapter, sleep=waits.append), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(waits, [60.0])

    def test_tty_countdown_fits_the_terminal_and_uses_a_real_deadline(self):
        clock = [0.0]

        class SlowTty(io.StringIO):
            def isatty(self):
                return True

            def fileno(self):
                return 1

            def write(self, text):
                written = super().write(text)
                if text.startswith("\r") and text.strip("\r "):
                    clock[0] += 0.6  # terminal rendering time is part of the wait
                return written

        stream = SlowTty()
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        with mock.patch(
                "assent.engine.os.get_terminal_size",
                return_value=os.terminal_size((48, 24))):
            engine._countdown(
                3, "Quota poll (every 30 minutes)", sleep,
                stream=stream, monotonic=lambda: clock[0])

        self.assertAlmostEqual(clock[0], 3.0)
        self.assertLess(sum(sleeps), 3.0)
        self.assertNotIn("\n", stream.getvalue())
        self.assertTrue(all(
            len(part) <= 47 for part in stream.getvalue().split("\r")))

    def test_interrupt_during_role_keeps_wip_and_journals_it(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()

        def interrupt(_prompt):
            (self.execution_root() / "interrupted.txt").write_text(
                "kept\n", encoding="utf-8")
            raise KeyboardInterrupt

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([interrupt])), 130)
        self.assertEqual(parse_task_file(path).status, "WIP")
        self.assertEqual(read_entries(parse_task_file(path).journal_path)[-1]["event"],
                         "interrupt")
        self.assertTrue((self.execution_root() / "interrupted.txt").is_file())

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result("resumed kept work")])), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_checkpoint_resume_restarts_the_same_role_without_waiting(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        checkpoint = TaskResult(
            1, '{"type":"assent.checkpoint_resume"}', False, None,
            checkpoint_resume=True)
        adapter = ScriptedAdapter([checkpoint, result("continued")])

        def forbidden_sleep(_seconds):
            raise AssertionError("checkpoint resume must not wait")

        self.assertEqual(self.run_with_contracts(
            cfg, adapter, sleep=forbidden_sleep), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(adapter.calls), 2)

    def test_exhaustion_is_human_decision_not_run_failure(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        adapter = ScriptedAdapter([result("no repair yet"), result("still failing")])

        self.assertEqual(self.run_with_contracts(cfg, adapter), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_exhausted_plan_workflow_persists_the_end_cursor(self):
        self.write_task(1, status="DONE", verify=_NEEDS_OK_TXT)
        cfg = self.build(extra_config=PLAN_ACTION_WORKFLOW)
        self.commit_all()

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([])), 0)

        state = read_workflow_state(cfg.tasks_dir)
        self.assertIsNotNone(state)
        self.assertEqual(state.step_index, cfg.plan_workflow_step_count)
        self.assertTrue(plan_workflow_requires_human(
            cfg.tasks_dir, cfg.plan_workflow_step_count))

    def test_todo_starts_a_fresh_workflow_after_rework(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=ACTION_ONLY_WORKFLOW)
        self.commit_all()
        write_workflow_state(cfg.tasks_dir, WorkflowState(
            "task", "t001", 1, False, "old-source",
            action="focused_test", action_status="FAILED",
            action_source_tree="old-tree", action_exit_code=1,
            action_evidence=("old command", "old failure")))

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([])), 0)

        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertIsNone(read_workflow_state(cfg.tasks_dir))

    def test_role_cannot_edit_task_contract(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()

        def tamper(_prompt):
            path.write_text(path.read_text(encoding="utf-8").replace(
                "做一件事。", "Different requirement"), encoding="utf-8")
            return result("changed the contract")

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([tamper])), 1)

    def test_read_only_role_cannot_edit_candidate_source(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(extra_config=SEPARATE_REVIEW_WORKFLOW)
        self.commit_all()

        def tamper(_prompt):
            target = self.execution_root() / "reviewer-edit.txt"
            target.write_text("not allowed\n", encoding="utf-8")
            return result("edited despite read-only responsibility")

        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([tamper])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")

    def test_unsettled_ignored_dirs_advance_without_running_focused_test(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        adapter = ScriptedAdapter([result(), result()])
        unknown = ignored_dirs.Decision(
            ignored_dirs.UNKNOWN, needs_review=True, inventory=("assets",))

        with mock.patch("assent.engine._ignored_dir_decision",
                        return_value=unknown), \
                mock.patch("assent.engine._verify_subprocess") as verify:
            code = self.run_with_contracts(cfg, adapter)

        self.assertEqual(code, 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        verify.assert_not_called()
        self.assertEqual(len(adapter.calls), 2)
        self.assertTrue(all("assent ignored-dirs declare" in call[0]
                            for call in adapter.calls))
        state = read_workflow_state(cfg.tasks_dir)
        self.assertIsNotNone(state)
        self.assertEqual(state.action, "")
        self.assertEqual(state.action_status, "")
        self.assertIn("focused_test not started", state.evidence[-1])

    def test_settled_ignored_dirs_allow_the_next_focused_test_to_run(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        adapter = ScriptedAdapter([result(), result()])
        reviewed = ignored_dirs.Decision(ignored_dirs.REVIEWED_NONE)
        unknown = ignored_dirs.Decision(
            ignored_dirs.UNKNOWN, needs_review=True, inventory=("assets",))
        decisions = [reviewed, reviewed, unknown, unknown, reviewed]
        passed = subprocess.CompletedProcess("verify", 0, "", "")

        with mock.patch("assent.engine._ignored_dir_decision",
                        side_effect=decisions), \
                mock.patch("assent.engine._verify_subprocess",
                           return_value=passed) as verify:
            code = self.run_with_contracts(cfg, adapter)

        self.assertEqual(code, 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        verify.assert_called_once()

    def test_a_later_focused_action_does_not_reuse_failed_evidence(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        failed = subprocess.CompletedProcess("verify", 1, "failed", "")
        passed = subprocess.CompletedProcess("verify", 0, "", "")

        with mock.patch("assent.engine._ignored_dir_decision",
                        return_value=ignored_dirs.Decision(
                            ignored_dirs.REVIEWED_NONE)), \
                mock.patch("assent.engine._verify_subprocess",
                           side_effect=[failed, passed]) as verify:
            code = self.run_with_contracts(
                cfg, ScriptedAdapter([result(), result()]))

        self.assertEqual(code, 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(verify.call_count, 2)

    def test_repeated_integration_pass_rechecks_only_the_scheduler_action(self):
        self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result()])), 0)
        target_ref, target, sources = engine._selection_snapshot((cfg,))
        evidence = FullVerifyEvidence(
            "PASSED", ("plan01",), target, sources, "candidate",
            "v" * 64, "s" * 64, 0)

        with mock.patch("assent.engine.verify_plan_action",
                        return_value=evidence) as verify, \
                mock.patch("assent.engine.get_adapter") as get_adapter:
            first_code = engine.run_selection_workflow(
                str(cfg.assent_dir / "assent.toml"), cfg.assent_dir,
                ["plan01"])
            second_code = engine.run_selection_workflow(
                str(cfg.assent_dir / "assent.toml"), cfg.assent_dir,
                ["plan01"])

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual(
            [call.kwargs["recheck"] for call in verify.call_args_list],
            [False, False])
        get_adapter.assert_not_called()
        state = read_selection_workflow_state(cfg.assent_dir)
        self.assertEqual(state.action_status, "PASSED")
        self.assertEqual(state.evidence, ())

    def test_failed_integration_action_advances_to_one_repair_session(self):
        self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result()])), 0)
        _target_ref, target, sources = engine._selection_snapshot((cfg,))
        failed = FullVerifyEvidence(
            "VERIFIER_FAILED", ("plan01",), target, sources, "candidate",
            "v" * 64, "s" * 64, 1, ("full verifier failed",))
        passed = FullVerifyEvidence(
            "PASSED", ("plan01",), target, sources, "candidate",
            "v" * 64, "s" * 64, 0)
        repair = ScriptedAdapter([result("repair session completed")])

        with mock.patch("assent.engine.verify_plan_action",
                        side_effect=[failed, passed]) as verify, \
                mock.patch("assent.engine.get_adapter", return_value=repair):
            code = engine.run_selection_workflow(
                str(cfg.assent_dir / "assent.toml"), cfg.assent_dir,
                ["plan01"])

        self.assertEqual(code, 0)
        self.assertEqual(
            [call.kwargs["recheck"] for call in verify.call_args_list],
            [False, True])
        self.assertEqual(len(repair.calls), 1)
        self.assertIn("full verifier failed", repair.calls[0][0])
        self.assertIn("Configured AI session: 1 of 1", repair.calls[0][0])
        self.assertIn("Do not narrate plans, internal deliberation, or rhetorical "
                      "questions.", repair.calls[0][0])
        state = read_selection_workflow_state(cfg.assent_dir)
        self.assertEqual(state.action_status, "PASSED")
        self.assertIn("repair session completed", state.evidence[0])

    def test_integration_exhaustion_reports_the_human_next_step(self):
        self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result()])), 0)
        _target_ref, target, sources = engine._selection_snapshot((cfg,))
        failed = FullVerifyEvidence(
            "VERIFIER_FAILED", ("plan01",), target, sources, "candidate",
            "v" * 64, "s" * 64, 1, ("full verifier failed",))

        output = io.StringIO()
        with mock.patch("assent.engine.verify_plan_action",
                        return_value=failed), \
                mock.patch("assent.engine.get_adapter",
                           return_value=ScriptedAdapter([result()])), \
                contextlib.redirect_stdout(output):
            code = engine.run_selection_workflow(
                str(cfg.assent_dir / "assent.toml"), cfg.assent_dir,
                ["plan01"])

        self.assertEqual(code, 0)
        self.assertIn(
            "Assent completed all configured automated work, but full "
            "verification did not pass.", output.getvalue())
        self.assertIn("rework before `assent accept`", output.getvalue())
        self.assertNotIn("REVIEW UNRESOLVED", output.getvalue())

        conflict_output = io.StringIO()
        with contextlib.redirect_stdout(conflict_output):
            engine._integration_automated_work_complete("PEER_CONFLICT")
        self.assertIn(
            "cross-plan conflicts still prevent full verification",
            conflict_output.getvalue())

    def test_target_conflict_is_repaired_then_full_verify_is_rebuilt(self):
        self.write_task(1)
        (self.root / "conflict.txt").write_text("base\n", encoding="utf-8")
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result()])), 0)

        source_worktree = self.execution_root()
        (source_worktree / "conflict.txt").write_text(
            "source\n", encoding="utf-8")
        self._git_execution("add", "conflict.txt")
        self._git_execution("commit", "-m", "source conflict")
        source_before = gitops.commit_of(source_worktree, "HEAD")

        (self.root / "conflict.txt").write_text("target\n", encoding="utf-8")
        self._git("add", "conflict.txt")
        self._git("commit", "-m", "target conflict")
        target = gitops.commit_of(self.root, "HEAD")
        failed = FullVerifyEvidence(
            "TARGET_CONFLICT", ("plan01",), target, (source_before,),
            gitops.tree_of(self.root, target), "v" * 64, "s" * 64, 1,
            ("Integration conflict: conflict.txt", "plan01:conflict.txt"))
        calls = 0

        def verify(_cfg, *, recheck=False):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.assertFalse(recheck)
                return failed
            self.assertTrue(recheck)
            _ref, current_target, sources = engine._selection_snapshot((cfg,))
            return FullVerifyEvidence(
                "PASSED", ("plan01",), current_target, sources,
                gitops.tree_of(source_worktree, sources[0]),
                "v" * 64, "s" * 64, 0)

        def repair(prompt):
            managed = gitops.reconcile_worktree_path(self.root, "plan01")
            self.assertIn(str(managed), prompt)
            self.assertIn("Exact conflict paths:\n- conflict.txt", prompt)
            self.assertIn("Do not narrate plans, internal deliberation, or "
                          "rhetorical questions.", prompt)
            (managed / "conflict.txt").write_text(
                "resolved\n", encoding="utf-8")
            return result("resolved the source-target conflict")

        adapter = ScriptedAdapter([repair])
        with mock.patch("assent.engine.verify_plan_action",
                        side_effect=verify), \
                mock.patch("assent.engine.get_adapter", return_value=adapter):
            code = engine.run_selection_workflow(
                str(cfg.assent_dir / "assent.toml"), cfg.assent_dir,
                ["plan01"])

        self.assertEqual(code, 0)
        self.assertEqual(calls, 2)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(gitops.commit_of(self.root, "HEAD"), target)
        source_after = gitops.commit_of(source_worktree, "HEAD")
        self.assertEqual(
            gitops.commit_parents(source_worktree, source_after),
            (source_before, target))
        self.assertEqual(
            (source_worktree / "conflict.txt").read_text(encoding="utf-8"),
            "resolved\n")

    def test_integration_billing_failure_returns_a_normal_failure(self):
        self.write_task(1)
        cfg = self.build(extra_config=WORKFLOW)
        self.commit_all()
        self.assertEqual(self.run_with_contracts(
            cfg, ScriptedAdapter([result()])), 0)
        _target_ref, target, sources = engine._selection_snapshot((cfg,))
        failed = FullVerifyEvidence(
            "VERIFIER_FAILED", ("plan01",), target, sources, "candidate",
            "v" * 64, "s" * 64, 1, ("full verifier failed",))
        billing = TaskResult(
            1, "credit balance is too low", False, None,
            failure_kind="billing")

        with mock.patch("assent.engine.verify_plan_action",
                        return_value=failed), \
                mock.patch("assent.engine.get_adapter",
                           return_value=ScriptedAdapter([billing])):
            code = engine.run_selection_workflow(
                str(cfg.assent_dir / "assent.toml"), cfg.assent_dir,
                ["plan01"])

        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
