"""Run and acceptance gates for source-bound after-plan runtime evidence."""

import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from assent import batch_accept, engine, gitops, runtime_test
from assent.accept import accept_plan
from assent.config import load_config
from assent.plan import (WorkflowState, runtime_test_workflow_state_path,
                         selection_workflow_state_path,
                         write_runtime_test_workflow_state)
from assent.verification_common import FullVerifyEvidence
from tests.engine_support import EngineTestCase, task_text


class RuntimeTestRunTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.write_task(1, status="DONE")
        self.cfg = self.build(extra_config=(
            '[workflow]\n'
            'task = [{ action = "focused_test" }]\n'
            'integration = [{ action = "full_verify" }]\n'
            'runtime_test = [{ action = "runtime_test" }]\n'))
        self.commit_all()
        engine._prepare_worktree(self.cfg)
        self.events = []

    def _contract(self, execution, command='python -c "raise SystemExit(0)"'):
        text = f'execution = {json.dumps(execution)}\n'
        if execution != "disabled":
            text += f'command = {json.dumps(command)}\n'
        (self.plan_dir / "_runtime_test.toml").write_text(
            text, encoding="utf-8")

    def _record(self, cfg, status="PASSED", source_tip=None):
        contract = engine.parse_runtime_test_contract(cfg.tasks_dir)
        assert contract.command is not None
        if source_tip is None:
            _branch, source_tip, _worktree = engine.source_snapshot(
                cfg, gitops.main_worktree(cfg.root))
        state = WorkflowState(
            "runtime_test", "", 1, False, source_tip,
            action="runtime_test", action_status=status,
            action_source_tree=runtime_test.evidence_identity(
                source_tip, contract.command),
            action_exit_code=0 if status == "PASSED" else 7,
            action_evidence=(contract.command, status.lower()))
        write_runtime_test_workflow_state(cfg.tasks_dir, state)

    def _fake_runtime(self, cfg, **_kwargs):
        self.events.append(f"runtime:{cfg.tasks_name}")
        self._record(cfg)
        return 0

    def _fake_verify(self, cfg, *, recheck=False):
        self.events.append(f"full_verify:{cfg.tasks_name}")
        _ref, target, sources = engine._selection_snapshot((cfg,))
        return FullVerifyEvidence(
            "PASSED", (cfg.tasks_name,), target, sources, "candidate",
            "v" * 64, "i" * 64, 0)

    def _run_selection(self):
        with mock.patch.object(engine, "run_runtime_test",
                               side_effect=self._fake_runtime), \
                mock.patch.object(engine, "verify_plan_action",
                                  side_effect=self._fake_verify):
            return engine.run_selection_workflow(
                str(self.cfg.assent_dir / "assent.toml"),
                self.cfg.assent_dir, ["plan01"])

    def test_only_after_plan_runs_between_plan_and_full_verify_layers(self):
        for execution, expected in (
                ("after_plan", ["runtime:plan01", "full_verify:plan01"]),
                ("explicit", ["full_verify:plan01"]),
                ("disabled", ["full_verify:plan01"])):
            with self.subTest(execution=execution):
                self.events.clear()
                runtime_test_workflow_state_path(self.plan_dir).unlink(
                    missing_ok=True)
                selection_workflow_state_path(self.cfg.assent_dir).unlink(
                    missing_ok=True)
                self._contract(execution)
                self.assertEqual(self._run_selection(), 0)
                self.assertEqual(self.events, expected)

    def test_unresolved_runtime_gate_prevents_full_verify(self):
        self._contract("after_plan")

        def fail_runtime(cfg, **_kwargs):
            self.events.append("runtime:failed")
            self._record(cfg, "FAILED")
            return 0

        output = io.StringIO()
        with mock.patch.object(engine, "run_runtime_test",
                               side_effect=fail_runtime), \
                mock.patch.object(engine, "verify_plan_action") as verify, \
                contextlib.redirect_stdout(output):
            code = engine.run_selection_workflow(
                str(self.cfg.assent_dir / "assent.toml"),
                self.cfg.assent_dir, ["plan01"])

        self.assertEqual(code, 0)
        verify.assert_not_called()
        self.assertIn("runtime-test gate is unresolved", output.getvalue())
        self.assertFalse((self.plan_dir / "_verification.toml").exists())

    def test_current_evidence_is_reused_but_a_new_source_commit_reruns(self):
        self._contract("after_plan")
        self.assertEqual(self._run_selection(), 0)
        self.assertEqual(self._run_selection(), 0)
        self.assertEqual(self.events.count("runtime:plan01"), 1)

        self._git_execution(
            "commit", "--allow-empty", "-m", "integration repair")

        self.assertEqual(self._run_selection(), 0)
        self.assertEqual(self.events.count("runtime:plan01"), 2)
        self.assertEqual(self.events[-2:],
                         ["runtime:plan01", "full_verify:plan01"])

    def test_multi_plan_gate_uses_each_plan_command_and_reruns_only_stale(self):
        second = self.cfg.assent_dir / "plan02"
        second.mkdir()
        (second / "t001_task.e.toml").write_text(
            task_text(status="DONE"), encoding="utf-8")
        first_command = 'python -c "print(1)"'
        second_command = 'python -c "print(2)"'
        self._contract("after_plan", first_command)
        (second / "_runtime_test.toml").write_text(
            'execution = "after_plan"\n'
            f'command = {json.dumps(second_command)}\n', encoding="utf-8")
        cfg2 = load_config(self.cfg.assent_dir / "assent.toml", "plan02")
        tips = {"plan01": "a" * 40, "plan02": "b" * 40}
        self._record(self.cfg, source_tip=tips["plan01"])
        stale = tips["plan02"][:-1] + "c"
        self._record(cfg2, source_tip=stale)

        def snapshot(cfg, _main):
            return "branch", tips[cfg.tasks_name], Path("worktree")

        commands = []

        def run_one(cfg, **_kwargs):
            commands.append(engine.parse_runtime_test_contract(
                cfg.tasks_dir).command)
            self._record(cfg, source_tip=tips[cfg.tasks_name])
            return 0

        with mock.patch.object(engine, "source_snapshot",
                               side_effect=snapshot), \
                mock.patch.object(engine, "run_runtime_test",
                                  side_effect=run_one):
            code, problem = engine.ensure_selection_runtime_tests(
                (self.cfg, cfg2), sleep=lambda _seconds: None)

        self.assertEqual((code, problem), (0, None))
        self.assertEqual(commands, [second_command])

    def test_accept_refuses_stale_runtime_evidence_before_receipt_reuse(self):
        self._contract("after_plan")
        self._record(self.cfg)
        self._git_execution("commit", "--allow-empty", "-m", "later source")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = accept_plan(self.cfg)

        self.assertEqual(code, 1)
        self.assertIn("runtime PASSED evidence is stale", output.getvalue())
        self.assertIn("assent test plan01", output.getvalue())

    def test_batch_accept_gate_refuses_one_stale_selected_source(self):
        self._contract("after_plan")
        self._record(self.cfg)
        self._git_execution("commit", "--allow-empty", "-m", "batch drift")
        main = gitops.main_worktree(self.cfg.root)
        source = self.execution_root()
        tip = gitops.commit_of(source, "HEAD")
        live = batch_accept._BatchSource(
            "plan01", gitops.current_branch(source), tip, source, "tree")

        problem = batch_accept._batch_gate_problem(
            main, {"plan01": self.cfg}, [live],
            gitops.current_branch(main), gitops.commit_of(main, "HEAD"),
            mock.Mock())

        self.assertIn("runtime PASSED evidence is stale", problem)
        self.assertIn("assent test plan01", problem)


if __name__ == "__main__":
    import unittest
    unittest.main()
