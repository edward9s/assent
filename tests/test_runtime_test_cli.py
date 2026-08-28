"""CLI routing and read-only contract-gate tests for ``assent test``."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from assent import inspection
from assent.__main__ import _build_parser, main
from assent.adapters import Adapter
from assent.config import load_config


class RecordingAdapter(Adapter):
    """A capability-only adapter that never starts a process."""

    def __init__(self) -> None:
        self.requests = []

    def preflight(self, requests):
        self.requests.extend(requests)
        return []

    def probe_cli(self):
        return True, "test adapter"


class RuntimeTestCliTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.git_marker = self.root / ".git"
        self.git_marker.mkdir()
        self.user_home = self.root / "user-assent"
        self.user_home.mkdir()
        self.environment = patch.dict(
            os.environ, {"ASSENT_HOME": str(self.user_home)}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write_config(self, *, runtime_workflow: bool = True,
                     project_command: bool = True) -> Path:
        workflow = 'runtime_test = [{ action = "runtime_test" }]\n' \
            if runtime_workflow else ""
        runtime_config = ('[runtime_test]\ncommand = "not-used-by-check"\n'
                          if project_command else "")
        path = self.assent_dir / "assent.toml"
        path.write_text(
            '[adapter]\nname = "claude"\n'
            '[adapter.claude]\ncommand = "not-used-by-check"\n'
            '[adapter.claude.models]\n'
            'prime = "test-prime/high"\n'
            'core = "test-core/medium"\n'
            'lite = "test-lite/low"\n'
            '[workflow]\n'
            'task = [{ action = "focused_test" }]\n'
            + workflow
            + runtime_config,
            encoding="utf-8")
        return path

    def write_task(self, plan_name: str) -> Path:
        path = self.assent_dir / plan_name / "t001_task.e.toml"
        path.parent.mkdir()
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "lite"\n'
            'status = "DONE"\n'
            'verify = "not-used-by-check"\n'
            'goal = "Goal"\n'
            'acceptance = "Acceptance"\n',
            encoding="utf-8")
        return path

    def write_contract(self, plan_name: str, execution: str) -> None:
        path = self.assent_dir / plan_name / "_runtime_test.toml"
        text = f'execution = {json.dumps(execution)}\n'
        if execution != "disabled":
            text += 'command = "not-used-by-check"\n'
        path.write_text(text, encoding="utf-8")

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(argv)
        return code, output.getvalue()

    def check(self, cfg) -> tuple[int, str]:
        adapter = RecordingAdapter()
        output = io.StringIO()
        with patch.object(inspection.contracts, "contract_errors", return_value=[]), \
                patch.object(inspection, "get_adapter", return_value=adapter), \
                patch.object(inspection, "_git_read", return_value="true"), \
                patch.object(inspection, "worktree_configuration_errors",
                             return_value=[]), \
                contextlib.redirect_stdout(output):
            code = inspection.check(cfg)
        return code, output.getvalue()

    def test_parser_has_one_exact_optional_plan_for_test(self):
        parser = _build_parser()

        self.assertIsNone(parser.parse_args(["test"]).plan_name)
        parsed = parser.parse_args(["test", "plan01", "--config", "custom.toml"])
        self.assertEqual(parsed.plan_name, "plan01")
        self.assertEqual(parsed.config, "custom.toml")
        with self.assertRaises(SystemExit):
            parser.parse_args(["test", "one", "two"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["runtime-test", "plan01"])

        help_output = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(help_output):
            main(["test", "--help"])
        help_text = help_output.getvalue()
        for phrase in ("live plan", "command source", "candidate worktree",
                       "workflow step"):
            self.assertIn(phrase, help_text)

    def test_plan_and_main_test_have_distinct_dispatch_targets(self):
        config = self.write_config()
        self.write_task("plan01")
        self.write_contract("plan01", "explicit")

        with patch("assent.__main__.engine.run_runtime_test", return_value=1) as plan, \
                patch("assent.__main__.engine.run_main_runtime_test",
                      side_effect=AssertionError("main target used")):
            code, output = self.run_cli(
                ["test", "plan01", "--config", str(config)])
        self.assertEqual(code, 1)
        plan.assert_called_once()
        self.assertEqual(plan.call_args.args[0].tasks_name, "plan01")
        self.assertIn("Runtime test target: live plan plan01", output)
        self.assertIn("Runtime test command source:", output)
        self.assertIn("Runtime test candidate worktree:", output)
        self.assertIn("Runtime test workflow step:", output)

        with patch("assent.__main__.engine.run_main_runtime_test",
                   return_value=0) as main_test, \
                patch("assent.__main__.engine.run_runtime_test",
                      side_effect=AssertionError("plan target used")):
            code, output = self.run_cli(["test", "--config", str(config)])
        self.assertEqual(code, 0)
        main_test.assert_called_once()
        self.assertEqual(main_test.call_args.args[0].tasks_name, "main")
        self.assertIn("Runtime test target: current main", output)
        self.assertIn("project config [runtime_test].command", output)

    def test_plan_selection_is_exact_and_does_not_dispatch_fuzzy_names(self):
        config = self.write_config()
        self.write_task("plan01")
        self.write_contract("plan01", "disabled")

        with patch("assent.__main__.engine.run_runtime_test",
                   side_effect=AssertionError("must not dispatch")):
            code, output = self.run_cli(
                ["test", "plan", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("unresolved", output)

    def test_test_returns_runtime_result_and_maps_interrupt_to_130(self):
        config = self.write_config()
        self.write_task("plan01")
        self.write_contract("plan01", "explicit")
        cases = ((0, "PASSED", 0), (0, "FAILED", 1),
                 (7, None, 1), (130, None, 130))
        for result, status, expected in cases:
            with self.subTest(result=result, status=status), patch(
                    "assent.__main__.engine.run_runtime_test",
                    return_value=result), patch(
                    "assent.__main__.read_runtime_test_workflow_state",
                    return_value=(SimpleNamespace(step_index=0,
                                                   action_status=status)
                                  if status is not None else None)):
                code, _output = self.run_cli(
                    ["test", "plan01", "--config", str(config)])
            self.assertEqual(code, expected)
        with patch("assent.__main__.engine.run_runtime_test",
                   side_effect=KeyboardInterrupt):
            code, output = self.run_cli(
                ["test", "plan01", "--config", str(config)])
        self.assertEqual(code, 130)
        self.assertIn("Test interrupted", output)

    def test_check_requires_contract_and_runtime_workflow_only_when_enabled(self):
        config = self.write_config(runtime_workflow=True)
        self.write_task("plan01")
        self.write_contract("plan01", "explicit")
        cfg = load_config(config, "plan01")
        code, output = self.check(cfg)
        self.assertEqual(code, 0)
        self.assertIn("Runtime-test contract: OK", output)
        self.assertIn("Runtime-test workflow: OK", output)

        self.write_contract("plan01", "disabled")
        config = self.write_config(runtime_workflow=False,
                                   project_command=False)
        cfg = load_config(config, "plan01")
        code, output = self.check(cfg)
        self.assertEqual(code, 0)
        self.assertIn("execution = disabled", output)
        self.assertIn("workflow: not required", output)

    def test_check_reports_missing_contract_or_workflow_without_running_command(self):
        config = self.write_config(runtime_workflow=True)
        self.write_task("plan01")
        cfg = load_config(config, "plan01")
        code, output = self.check(cfg)
        self.assertEqual(code, 1)
        self.assertIn("Runtime-test contract: FAIL", output)

        self.write_contract("plan01", "after_plan")
        config = self.write_config(runtime_workflow=False)
        cfg = load_config(config, "plan01")
        code, output = self.check(cfg)
        self.assertEqual(code, 1)
        self.assertIn("Runtime-test workflow: FAIL", output)
        self.assertFalse((self.root / "not-used-by-check").exists())

    def test_check_all_aggregates_a_missing_contract(self):
        config = self.write_config(runtime_workflow=False)
        self.write_task("good")
        self.write_task("old")
        self.write_contract("good", "disabled")
        adapter = RecordingAdapter()
        output = io.StringIO()
        with patch.object(inspection.contracts, "contract_errors", return_value=[]), \
                patch.object(inspection, "get_adapter", return_value=adapter), \
                patch.object(inspection, "_git_read", return_value="true"), \
                patch.object(inspection, "worktree_configuration_errors",
                             return_value=[]), \
                contextlib.redirect_stdout(output):
            code = main(["check", "--config", str(config)])
        self.assertEqual(code, 1)
        text = output.getvalue()
        self.assertIn("Plan dependency graph: OK", text)
        self.assertIn("plan = good", text)
        self.assertIn("plan = old", text)
        self.assertIn("Runtime-test contract: FAIL", text)

    def test_check_does_not_need_a_git_worktree_to_report_the_contract(self):
        config = self.write_config(runtime_workflow=False)
        self.write_task("plan01")
        self.write_contract("plan01", "disabled")
        self.git_marker.rmdir()
        cfg = load_config(config, "plan01")
        adapter = RecordingAdapter()
        output = io.StringIO()
        with patch.object(inspection.contracts, "contract_errors", return_value=[]), \
                patch.object(inspection, "get_adapter", return_value=adapter), \
                patch.object(inspection, "_git_read", return_value=None), \
                contextlib.redirect_stdout(output):
            code = inspection.check(cfg)
        self.assertEqual(code, 1)
        self.assertIn("Runtime-test contract: OK", output.getvalue())
        self.assertIn("no git repository", output.getvalue())
        self.assertFalse((self.root / "not-used-by-check").exists())


if __name__ == "__main__":
    unittest.main()
