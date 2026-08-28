import contextlib
import io
import json
from pathlib import Path
from unittest import mock

from assent import engine
from assent.adapters import TaskResult
from assent.plan import read_runtime_test_workflow_state
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result


class RuntimeTestPlanTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.write_task(1, status="DONE")
        (self.root / "src").mkdir()
        (self.root / "src" / "value.txt").write_text("bad\n", encoding="utf-8")
        self.commit_all()

    def build_runtime(self, command: str, *, extra_role: bool = True,
                      config_command: str | None = None):
        entries = ['{ action = "runtime_test" }']
        if extra_role:
            entries += ['{ role = "writer", model = "lite" }',
                        '{ action = "runtime_test" }']
        config_runtime = ("\n[runtime_test]\ncommand = "
                          f'{json.dumps(config_command)}\n'
                          if config_command else "")
        cfg = self.build(extra_config=(
            "[workflow]\n"
            "task = [{ action = \"focused_test\" }]\n"
            f"runtime_test = [{', '.join(entries)}]\n"
            "[abilities.write]\nprompt = \"Repair the runtime failure.\"\n"
            "writes = true\n"
            "[roles.writer]\nability = [\"write\"]\n"
            + config_runtime))
        (self.plan_dir / "_runtime_test.toml").write_text(
            f'execution = "explicit"\ncommand = {json.dumps(command)}\n',
            encoding="utf-8")
        return cfg

    def run_runtime(self, cfg, adapter):
        output = io.StringIO()
        with mock.patch.object(engine.contracts, "require_contracts"), \
                contextlib.redirect_stdout(output):
            code = engine.run_runtime_test(
                cfg, adapter=adapter, sleep=lambda _seconds: None)
        return code, output.getvalue()

    def test_initial_pass_uses_contract_command_in_plan_worktree(self):
        command = ('python -c "import pathlib,sys;sys.exit('
                   "0 if pathlib.Path('src/value.txt').read_text().strip() == "
                   "'bad' else 7)\"")
        cfg = self.build_runtime(
            command, extra_role=False,
            config_command='python -c "raise SystemExit(9)"')
        (self.plan_dir / "_runtime_test.toml").write_text(
            f'execution = "after_plan"\ncommand = {json.dumps(command)}\n',
            encoding="utf-8")
        adapter = ScriptedAdapter([])

        code, _output = self.run_runtime(cfg, adapter)

        self.assertEqual(code, 0)
        self.assertEqual(adapter.calls, [])
        state = read_runtime_test_workflow_state(self.plan_dir)
        self.assertEqual(state.action_status, "PASSED")
        self.assertEqual(state.action_evidence[0], command)
        self.assertTrue(self.execution_root().is_dir())

    def test_failed_command_is_repaired_then_rerun(self):
        command = ('python -c "import pathlib,sys;sys.exit('
                   "0 if pathlib.Path('src/value.txt').read_text().strip() == "
                   "'good' else 4)\"")
        cfg = self.build_runtime(command)

        def repair(prompt):
            self.assertIn("Status: FAILED", prompt)
            self.assertIn("Exit code: 4", prompt)
            self.assertIn(command, prompt)
            (self.execution_root() / "src" / "value.txt").write_text(
                "good\n", encoding="utf-8")
            return ok_result()

        code, _output = self.run_runtime(cfg, ScriptedAdapter([repair]))

        self.assertEqual(code, 0)
        self.assertEqual(
            (self.execution_root() / "src" / "value.txt").read_text(
                encoding="utf-8"), "good\n")
        self.assertEqual(
            read_runtime_test_workflow_state(self.plan_dir).action_status,
            "PASSED")

    def test_successful_role_without_source_change_stops_unresolved(self):
        cfg = self.build_runtime('python -c "raise SystemExit(6)"')

        code, output = self.run_runtime(cfg, ScriptedAdapter([ok_result()]))

        self.assertEqual(code, 0)
        self.assertIn("writable role made no source change", output)
        state = read_runtime_test_workflow_state(self.plan_dir)
        self.assertEqual(state.action_status, "FAILED")
        self.assertEqual(state.step_index, 3)

    def test_exhausted_steps_preserve_failed_evidence_and_edits(self):
        cfg = self.build_runtime('python -c "raise SystemExit(5)"')

        def change(_prompt):
            (self.execution_root() / "src" / "attempt.txt").write_text(
                "kept\n", encoding="utf-8")
            return ok_result()

        code, output = self.run_runtime(cfg, ScriptedAdapter([change]))

        self.assertEqual(code, 0)
        self.assertIn("REVIEW UNRESOLVED, HUMAN DECISION", output)
        self.assertTrue((self.execution_root() / "src" / "attempt.txt").is_file())
        state = read_runtime_test_workflow_state(self.plan_dir)
        self.assertEqual(state.action_status, "FAILED")
        self.assertEqual(state.action_exit_code, 5)

    def test_primary_worktree_boundary_violation_refuses(self):
        cfg = self.build_runtime('python -c "raise SystemExit(2)"')

        def cross_boundary(_prompt):
            (self.root / "outside.txt").write_text("invalid\n", encoding="utf-8")
            return ok_result()

        code, output = self.run_runtime(
            cfg, ScriptedAdapter([cross_boundary]))

        self.assertEqual(code, 1)
        self.assertIn("primary worktree:outside.txt", output)

    def test_interrupted_role_resumes_before_runtime_action(self):
        counter = self.root.parent / f"{self.root.name}-runtime-count.txt"
        self.addCleanup(counter.unlink, missing_ok=True)
        command = (
            "python -c \"import pathlib,sys;p=pathlib.Path(r'"
            + str(counter) + "');p.write_text(p.read_text()+'x' if p.exists() "
            "else 'x');sys.exit(0 if pathlib.Path('src/value.txt').read_text()"
            ".strip() == 'good' else 3)\"")
        cfg = self.build_runtime(command)

        code, _output = self.run_runtime(
            cfg, ScriptedAdapter([lambda _prompt: (_ for _ in ()).throw(
                KeyboardInterrupt())]))
        self.assertEqual(code, 130)
        self.assertEqual(counter.read_text(encoding="utf-8"), "x")

        def repair(_prompt):
            (self.execution_root() / "src" / "value.txt").write_text(
                "good\n", encoding="utf-8")
            return ok_result()

        code, _output = self.run_runtime(cfg, ScriptedAdapter([repair]))
        self.assertEqual(code, 0)
        self.assertEqual(counter.read_text(encoding="utf-8"), "xx")

    def test_new_manual_run_restarts_completed_workflow_without_other_writes(self):
        counter = self.root.parent / f"{self.root.name}-fresh-count.txt"
        self.addCleanup(counter.unlink, missing_ok=True)
        command = (
            "python -c \"import pathlib;p=pathlib.Path(r'" + str(counter)
            + "');p.write_text(p.read_text()+'x' if p.exists() else 'x')\"")
        cfg = self.build_runtime(command, extra_role=False)
        task_before = self.write_task(1, status="DONE").read_bytes()
        journal = self.plan_dir / "t001_task.journal.jsonl"
        journal_before = journal.read_bytes() if journal.exists() else None
        receipt = self.plan_dir / "_verification.toml"
        receipt.write_text("sentinel\n", encoding="utf-8")

        verifier = mock.patch.object(
            engine.verification, "verify_plan_if_needed",
            side_effect=AssertionError("full verify must not run"))
        accept = mock.patch("assent.accept.accept_plan",
                            side_effect=AssertionError("accept must not run"))
        with verifier, accept:
            self.assertEqual(self.run_runtime(cfg, ScriptedAdapter([]))[0], 0)
            self.assertEqual(self.run_runtime(cfg, ScriptedAdapter([]))[0], 0)

        self.assertEqual(counter.read_text(encoding="utf-8"), "xx")
        self.assertEqual((self.plan_dir / "t001_task.e.toml").read_bytes(), task_before)
        self.assertEqual(journal.read_bytes() if journal.exists() else None,
                         journal_before)
        self.assertEqual(receipt.read_text(encoding="utf-8"), "sentinel\n")

    def test_disabled_contract_is_refused_before_worktree_or_adapter(self):
        cfg = self.build_runtime('python -c "raise SystemExit(0)"')
        (self.plan_dir / "_runtime_test.toml").write_text(
            'execution = "disabled"\n', encoding="utf-8")
        adapter = ScriptedAdapter([TaskResult(0, "", False, None)])

        code, output = self.run_runtime(cfg, adapter)

        self.assertEqual(code, 1)
        self.assertIn("disabled", output)
        self.assertEqual(adapter.calls, [])
        self.assertFalse(self.execution_root() != self.root)


if __name__ == "__main__":
    import unittest
    unittest.main()
