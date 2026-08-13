"""CLI entry point and init tests. ``run`` tees to the work folder's
_assent.log, so tests always chdir into a temporary directory to avoid
dirtying the test process's own working directory.

Chinese literals that remain are deliberate user-authored data (task titles,
goals, acceptance text, rework reasons) used to prove that non-English data
passes through the CLI verbatim rather than being translated as output."""
import contextlib
import io
import importlib.metadata
import itertools
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from assent.__main__ import _start_stdin_stop_watcher, main
from assent.adapters.process import clear_stop_wake
from assent.config import load_config
from assent.init import init as run_init
from assent.plan import Plan
from tests.test_contracts import install_global_contracts
from tests.test_shared_paths import settle_shared_paths

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HAN_CHAR_RE = re.compile(r"[一-鿿]")


class MainTestCase(unittest.TestCase):
    def setUp(self):
        # A scheduler-spawned session exports ASSENT_STDIN_STOP to its child.
        # Inheriting it here would make every main() call in these tests start
        # the stop watcher on this process's own (non-interactive) stdin, whose
        # immediate EOF then raises KeyboardInterrupt inside an unrelated later
        # test.  The watcher's own behavior is covered by TestStdinStopWatcher,
        # which sets the variable explicitly.
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("ASSENT_STDIN_STOP", None)
        # The same spawned session also exports ASSENT_FOLDER_RUN, which would
        # otherwise relabel every timing line here as a scheduler child's.
        os.environ.pop("ASSENT_FOLDER_RUN", None)
        self.root = Path(tempfile.mkdtemp())
        self._old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._old_cwd)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # `run` refuses without the global contracts, and every layered config
        # read consults the user home, so both point at a temporary one here
        # rather than at whatever the developer happens to have installed.
        self.user_home = install_global_contracts(self)

    def run_main(self, argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()

    def write_config(self, text="") -> Path:
        config = self.root / ".assent" / "assent.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")
        return config

    def write_task(self, folder: str, status: str = "TODO") -> Path:
        path = self.root / ".assent" / folder / "t001_task.e.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'title = "任務"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "完成任務"\n'
            'acceptance = "驗證通過"\n',
            encoding="utf-8")
        return path


class TestDispatch(MainTestCase):
    def test_version_reports_installed_distribution_from_empty_directory(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(_PROJECT_ROOT)
        result = subprocess.run(
            [sys.executable, "-m", "assent", "--version"],
            cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", env=environment)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout, f"assent {importlib.metadata.version('assent')}\n")
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.root / ".assent").exists())

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_output_is_english_for_top_level_and_every_subcommand(self):
        commands = ("run", "status", "check", "report", "verify", "clean", "accept",
                    "reconcile", "reject", "rework", "init")
        for argv in (["--help"],) + tuple(
                [command, "--help"] for command in commands):
            with self.subTest(argv=argv):
                env = dict(os.environ)
                env["PYTHONPATH"] = str(_PROJECT_ROOT)
                result = subprocess.run(
                    [sys.executable, "-m", "assent", *argv],
                    cwd=self.root, capture_output=True, text=True,
                    encoding="utf-8", env=env)
                self.assertEqual(result.returncode, 0)
                self.assertNotRegex(result.stdout, _HAN_CHAR_RE)

    def test_shared_paths_operations_are_reachable_through_the_real_cli(self):
        """Both inspection and the only manifest writer are real subcommands.

        It also has to reach dispatch without a project `.assent`: it acts on
        the Git worktree it is run in, and a source worktree carries none.
        """
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_PROJECT_ROOT)
        result = subprocess.run(
            [sys.executable, "-m", "assent", "shared-paths", "review", "--help"],
            cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--path assets --path pkg", result.stdout)
        self.assertIn("--none --watch", result.stdout)
        self.assertIn("--watch is a repeatable", result.stdout)
        self.assertNotRegex(result.stdout, _HAN_CHAR_RE)

        result = subprocess.run(
            [sys.executable, "-m", "assent", "shared-paths", "status", "--help"],
            cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("without changing it", result.stdout)
        self.assertNotRegex(result.stdout, _HAN_CHAR_RE)

        with patch("assent.__main__.shared_paths_review",
                   return_value=0) as review:
            code, _out = self.run_main(
                ["shared-paths", "review", "--path", "pkg",
                 "--watch", "pubspec.yaml"])
        self.assertEqual(code, 0)
        review.assert_called_once_with(["pkg"], ["pubspec.yaml"], False)

        with patch("assent.__main__.shared_paths_status",
                   return_value=0) as status:
            code, _out = self.run_main(["shared-paths", "status"])
        self.assertEqual(code, 0)
        status.assert_called_once_with()
        self.assertFalse((self.root / ".assent").exists())

    def test_missing_config_reports_error(self):
        code, out = self.run_main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("Config error", out)
        self.assertIn("assent init", out)

    def test_run_missing_config_reports_error(self):
        code, out = self.run_main(
            ["run", "--config", str(self.root / "nope" / "assent.toml")])
        self.assertEqual(code, 1)

    def test_explicit_selection_audits_every_name_before_dispatch(self):
        config = self.write_config()
        self.write_task("AA01", "DONE")
        cases = (
            (["run", "AA01", "BB01"], "assent.__main__.engine.run"),
            (["verify", "AA01", "BB01"],
             "assent.__main__.verify_selected_batch"),
            (["accept", "AA01", "BB01"],
             "assent.__main__.accept_selected_batch"),
            (["clean", "AA01", "BB01"], "assent.__main__.clean_folders"),
            (["archive", "AA01", "BB01"],
             "assent.__main__.archive_selected"),
            (["status", "BB01"], "assent.__main__.inspection.status"),
            (["check", "BB01"], "assent.__main__.inspection.check"),
            (["report", "BB01"], "assent.__main__.inspection.report"),
            (["reconcile", "BB01"], "assent.__main__.reconcile_start"),
            (["reject", "BB01"], "assent.__main__.reject_folder"),
            (["rework", "BB01", "t001"], "assent.__main__.rework_task"),
        )
        for argv, target in cases:
            with self.subTest(argv=argv), patch(target) as operation:
                code, out = self.run_main(
                    [*argv, "--config", str(config)])
            self.assertEqual(code, 1)
            self.assertIn("BB01", out)
            self.assertIn("unresolved", out)
            operation.assert_not_called()
        self.assertFalse((self.root / ".assent" / "BB01").exists())
        self.assertFalse(
            (self.root / ".assent" / "_archive" / "BB01.zip").exists())

    def test_explicit_selection_reports_all_unresolved_names_in_order(self):
        config = self.write_config()
        code, output = self.run_main([
            "run", "MISSING02", "MISSING01", "MISSING03", "--config",
            str(config)])
        self.assertEqual(code, 1)
        self.assertIn("MISSING02, MISSING01", output)
        self.assertFalse((self.root / ".assent" / "MISSING01").exists())
        self.assertFalse((self.root / ".assent" / "MISSING02").exists())
        self.assertFalse((self.root / ".assent" / "MISSING03").exists())

    def test_run_refuses_before_dispatch_when_a_global_contract_is_broken(self):
        config = self.write_config()
        self.write_task("plan01")
        cases = {"missing": lambda p: p.unlink(),
                 "stale": lambda p: p.write_text("older\n", encoding="utf-8")}
        for state, break_contract in cases.items():
            for argv in (["run"], ["run", "plan01"], ["run", "--all"]):
                with self.subTest(state=state, argv=argv):
                    home = install_global_contracts(self)
                    break_contract(home / "instructions.md")
                    with patch("assent.__main__.engine.run",
                               side_effect=AssertionError("must not dispatch")), \
                            patch("assent.__main__.run_all",
                                  side_effect=AssertionError("must not dispatch")):
                        code, out = self.run_main(
                            [*argv, "--config", str(config)])
                    self.assertEqual(code, 1)
                    self.assertIn("Global contracts: FAIL", out)
                    self.assertIn("assent init", out)

    def test_an_absent_default_project_file_is_not_an_error(self):
        # Everything stated user-wide is a complete, ordinary setup.
        (self.user_home / "assent.toml").write_text(
            '[adapter]\nname = "claude"\n', encoding="utf-8")
        self.write_task("plan01")
        self.assertFalse((self.root / ".assent" / "assent.toml").exists())

        code, out = self.run_main(["status"])
        self.assertEqual(code, 0)
        self.assertNotIn("Config error", out)

    def test_config_help_presents_the_project_file_as_an_optional_override(self):
        out = io.StringIO()
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(out):
            main(["run", "--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("Optional project settings file", text)
        self.assertIn("~/.assent/assent.toml", text)

    def test_run_all_accepts_an_explicit_prefix(self):
        config = self.write_config()
        self.write_task("work")
        with patch("assent.__main__.engine.run", return_value=0), patch(
                "assent.__main__.run_all", return_value=0) as mocked, patch(
                "assent.__main__.verify_batch",
                return_value=0):
            code, _ = self.run_main(
                ["run", "work", "--all", "--config", str(config)])
        self.assertEqual(code, 0)
        mocked.assert_called_once()

    def test_run_jobs_requires_all_and_positive_number(self):
        for argv in (["run", "--jobs", "2"],
                     ["run", "--all", "--jobs", "0"]):
            with self.subTest(argv=argv), self.assertRaises(
                    SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_run_all_dispatches_with_default_jobs(self):
        config = self.write_config()
        with patch("assent.__main__.run_all", return_value=0) as mocked, \
                patch("assent.__main__.verify_batch", return_value=0):
            code, _ = self.run_main(["run", "--all", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[2], 1)

    def test_removed_auto_fix_option_is_a_usage_error(self):
        config = self.write_config()
        self.write_task("work")
        with self.assertRaises(SystemExit) as ctx, \
                contextlib.redirect_stderr(io.StringIO()):
            main(["run", "work", "--auto-fix", "--config", str(config)])
        self.assertEqual(ctx.exception.code, 2)

    def test_run_named_folders_dispatch_in_given_order(self):
        config = self.write_config()
        self.write_task("first")
        self.write_task("second")
        with patch("assent.__main__.engine.run", return_value=0) as mocked:
            code, _ = self.run_main(
                ["run", "first", "second", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(
            [call.args[0].tasks_name for call in mocked.call_args_list],
            ["first", "second"])

    def test_run_named_folders_stops_after_first_failure(self):
        config = self.write_config()
        self.write_task("first")
        self.write_task("second")
        with patch("assent.__main__.engine.run", side_effect=[1, 0]) as mocked:
            code, _ = self.run_main(
                ["run", "first", "second", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual(
            [call.args[0].tasks_name for call in mocked.call_args_list],
            ["first"])

    def test_run_named_folders_with_all_runs_remainder_once(self):
        config = self.write_config()
        self.write_task("first")
        self.write_task("second")
        with patch("assent.__main__.engine.run", return_value=0) as run_mock, \
                patch("assent.__main__.run_all", return_value=0) as all_mock, \
                patch("assent.__main__.verify_batch",
                      return_value=0):
            code, _ = self.run_main([
                "run", "first", "second", "--all", "--jobs", "3",
                "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(
            [call.args[0].tasks_name for call in run_mock.call_args_list],
            ["first", "second"])
        all_mock.assert_called_once_with(str(config), config.parent.resolve(), 3)

    def test_run_named_folders_rejects_duplicates(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(
                io.StringIO()):
            main(["run", "first", "first"])
        self.assertEqual(ctx.exception.code, 2)

    def test_run_named_folders_rejects_single_folder_options(self):
        cases = (
            ["run", "first", "second", "--once"],
            ["run", "first", "second", "--task", "t001"],
            ["run", "first", "--jobs", "2"],
            ["run", "first", "second", "--jobs", "2"],
            ["run", "first", "--all", "--once"],
            ["run", "first", "--all", "--task", "t001"],
        )
        for argv in cases:
            with self.subTest(argv=argv), self.assertRaises(
                    SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_all_plan_commands_accept_folder_override(self):
        config = self.write_config()
        assent_dir = config.parent
        self.write_task("B")
        commands = (("run", "engine"), ("status", "inspection"),
                    ("check", "inspection"), ("report", "inspection"))
        for command, owner in commands:
            with self.subTest(command=command), patch(
                    f"assent.__main__.{owner}.{command}", return_value=0) as mocked:
                code, _ = self.run_main([command, "B", "--config", str(config)])
                self.assertEqual(code, 0)
                cfg = mocked.call_args.args[0]
                self.assertEqual(cfg.tasks_name, "B")
                self.assertEqual(cfg.tasks_dir, assent_dir.resolve() / "B")

    def test_clean_accepts_folder_override_and_config_option(self):
        config = self.write_config()
        self.write_task("B")
        with patch("assent.__main__.clean_folders", return_value=0) as mocked:
            code, _ = self.run_main(["clean", "B", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual([cfg.tasks_name for cfg in mocked.call_args.args[0]], ["B"])

    def test_clean_without_folder_uses_all_task_folders(self):
        config = self.write_config()
        self.write_task("beta")
        self.write_task("alpha")
        with patch("assent.__main__.clean_folders", return_value=0) as mocked:
            code, _ = self.run_main(["clean", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual([cfg.tasks_name for cfg in mocked.call_args.args[0]],
                         ["alpha", "beta"])

    def test_clean_has_no_force_option(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(
                io.StringIO()):
            main(["clean", "--force"])
        self.assertEqual(ctx.exception.code, 2)

    def test_accept_requires_a_folder_and_has_no_remote_options(self):
        for argv in (["accept"], ["accept", "one", "--all"],
                     ["accept", "one", "two", "--all"],
                     ["accept", "one", "one"],
                     ["accept", "one", "--push"]):
            with self.subTest(argv=argv), self.assertRaises(
                    SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_accept_dispatches_explicit_folder(self):
        config = self.write_config()
        self.write_task("reviewed", "DONE")
        with patch("assent.__main__.accept_folder", return_value=0) as mocked:
            code, _ = self.run_main(
                ["accept", "reviewed", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "reviewed")

    def test_accept_dispatches_two_or_more_folders_as_selected_batch(self):
        config = self.write_config()
        self.write_task("child", "DONE")
        self.write_task("parent", "DONE")
        with patch("assent.__main__.accept_selected_batch", return_value=0) as mocked:
            code, _ = self.run_main(
                ["accept", "child", "parent", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args,
                         (str(config), config.parent.resolve(),
                          ["child", "parent"]))

    def test_verify_requires_a_mode_and_rejects_incompatible_options(self):
        for argv in (["verify"], ["verify", "one", "--all"],
                     ["verify", "one", "two", "--focus"],
                     ["verify", "one", "--focus", "--no-bisect"],
                     ["verify", "one", "one"]):
            with self.subTest(argv=argv), self.assertRaises(
                    SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_verify_dispatches_exact_selection_workflow(self):
        config = self.write_config()
        self.write_task("reviewed", "DONE")
        with patch("assent.__main__.engine.run_selection_workflow",
                   side_effect=[0, 1]) as workflow:
            codes = [self.run_main(
                ["verify", "reviewed", "--config", str(config)])[0]
                     for _ in range(2)]
        self.assertEqual(codes, [0, 1])
        self.assertEqual(workflow.call_count, 2)
        workflow.assert_called_with(
            str(config), config.parent.resolve(), ["reviewed"])

    def test_verify_dispatches_selected_and_focused_forms(self):
        config = self.write_config()
        self.write_task("later", "DONE")
        self.write_task("earlier", "DONE")
        with patch("assent.__main__.engine.run_selection_workflow",
                   return_value=0) as workflow:
            code, _ = self.run_main([
                "verify", "later", "earlier", "--config", str(config)])
        self.assertEqual(code, 0)
        workflow.assert_called_once_with(
            str(config), config.parent.resolve(), ["later", "earlier"])

        with patch("assent.__main__.engine.verify_focused",
                   return_value=0) as focus:
            code, _ = self.run_main([
                "verify", "later", "--focus", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(focus.call_args.args[0].tasks_name, "later")

    def test_verify_batch_dispatches_dynamic_integration(self):
        config = self.write_config()
        with patch("assent.__main__.engine.run_dynamic_selection_workflow",
                   return_value=0) as workflow:
            code, _ = self.run_main([
                "verify", "--batch", "--config", str(config)])
        self.assertEqual(code, 0)
        workflow.assert_called_once_with(str(config), config.parent.resolve())

    def test_verify_batch_help_states_the_conflict_skip_confirmation(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(
                output):
            main(["verify", "-h"])
        self.assertEqual(ctx.exception.code, 0)
        text = " ".join(output.getvalue().split())
        self.assertIn("a conflicting source is reported and, after one "
                      "confirmation, skipped together with the plans queued "
                      "after it", text)
        self.assertNotIn("accept the plans ahead", text)

    def test_verify_interrupt_returns_130(self):
        config = self.write_config()
        self.write_task("reviewed", "DONE")
        with patch("assent.__main__.engine.run_selection_workflow",
                   side_effect=KeyboardInterrupt), \
                patch("assent.__main__.inspection.try_write_report") as report:
            code, out = self.run_main(
                ["verify", "reviewed", "--config", str(config)])
        self.assertEqual(code, 130)
        self.assertIn("temporary resources were cleaned up", out)
        report.assert_not_called()

    def test_no_push_subcommand_exists(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(
                io.StringIO()):
            main(["push"])
        self.assertEqual(ctx.exception.code, 2)

    def test_reject_requires_folder(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(
                io.StringIO()):
            main(["reject"])
        self.assertEqual(ctx.exception.code, 2)

    def test_reject_dispatches_to_reject_folder(self):
        config = self.write_config()
        self.write_task("B")
        with patch("assent.__main__.reject_folder", return_value=0) as mocked:
            code, _ = self.run_main(["reject", "B", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "B")

    def test_reconcile_routes_each_form_to_its_own_lifecycle(self):
        config = self.write_config()
        self.write_task("stuck", "DONE")
        forms = (([], "reconcile_start"),
                 (["--continue"], "reconcile_continue"),
                 (["--abort"], "reconcile_abort"))
        for flags, function in forms:
            with self.subTest(flags=flags), patch(
                    f"assent.__main__.{function}", side_effect=[0, 1]) as mocked:
                codes = [self.run_main(
                    ["reconcile", "stuck", *flags, "--config", str(config)])[0]
                         for _ in range(2)]
                self.assertEqual(codes, [0, 1])
                self.assertEqual(mocked.call_args.args[0].tasks_name, "stuck")

    def test_reconcile_refuses_contradictory_or_missing_arguments(self):
        for argv in (["reconcile"],
                     ["reconcile", "one", "two"],
                     ["reconcile", "one", "--continue", "--abort"],
                     ["reconcile", "--continue"],
                     ["reconcile", "one", "--focus"],
                     ["reconcile", "one", "--all"]):
            with self.subTest(argv=argv), self.assertRaises(
                    SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_reconcile_help_states_the_verification_boundary(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(output):
            main(["reconcile", "-h"])
        self.assertEqual(ctx.exception.code, 0)
        text = output.getvalue()
        self.assertIn("PLAN", text)
        for option in ("--continue", "--abort", "--config"):
            self.assertIn(option, text)
        # argparse wraps the description, so compare without its line breaks.
        unwrapped = " ".join(text.split())
        self.assertIn("never runs the focused or the complete verification",
                      unwrapped)
        self.assertIn("assent verify PLAN", unwrapped)
        self.assertIn("assent accept PLAN", unwrapped)

    def test_reconcile_configuration_error_returns_one_without_dispatch(self):
        config = self.write_config()
        with patch("assent.__main__.reconcile_start") as mocked:
            code, out = self.run_main(
                ["reconcile", "bad/name", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("Folder selection refused", out)
        mocked.assert_not_called()

    def test_rework_help_shows_only_formal_syntax_and_options(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(output):
            main(["rework", "-h"])
        self.assertEqual(ctx.exception.code, 0)
        text = output.getvalue()
        self.assertIn("assent rework", text)
        self.assertIn("PLAN TASK", text)
        for option in ("--cascade", "--revert-code", "--reason", "--config"):
            self.assertIn(option, text)
        for forbidden in ("--all", "--once"):
            self.assertNotIn(forbidden, text)
        self.assertIn("Keeps code by default", text)
        self.assertIn("contiguous branch tail", text)

    def test_rework_requires_folder_and_task_and_rejects_unknown_options(self):
        for argv in (["rework"], ["rework", "B"],
                     ["rework", "B", "t001", "--all"]):
            with self.subTest(argv=argv), self.assertRaises(
                    SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
                main(argv)
            self.assertEqual(ctx.exception.code, 2)

    def test_rework_dispatches_all_values_without_rewriting_exit_code(self):
        config = self.write_config()
        self.write_task("B")
        with patch("assent.__main__.rework_task", side_effect=[0, 1]) as mocked:
            codes = [self.run_main([
                "rework", "B", "t003", "--cascade", "--revert-code",
                "--reason", "驗收不符", "--config", str(config)])[0]
                     for _ in range(2)]
        self.assertEqual(codes, [0, 1])
        cfg, task = mocked.call_args.args
        self.assertEqual(cfg.tasks_name, "B")
        self.assertEqual(task, "t003")
        self.assertEqual(mocked.call_args.kwargs, {
            "cascade": True,
            "reason": "驗收不符",
            "revert_code": True,
        })

    def test_rework_configuration_error_returns_one_without_dispatch(self):
        config = self.write_config()
        with patch("assent.__main__.rework_task") as mocked:
            code, out = self.run_main([
                "rework", "bad/name", "t001", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("Folder selection refused", out)
        mocked.assert_not_called()

    def test_invalid_folder_override_reports_error(self):
        config = self.write_config()
        code, out = self.run_main(["status", "bad/name", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("Command-line task folder", out)

    def test_removed_plan_section_rejected_as_unknown_key(self):
        config = self.write_config('[plan]\ntasks = "A"\n')
        code, out = self.run_main(["status", "A", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("unknown top-level keys", out)

    def test_run_without_folder_selects_unique_ongoing_folder(self):
        config = self.write_config()
        self.write_task("active", "TODO")
        self.write_task("archive", "DONE")
        with patch("assent.__main__.engine.run", return_value=0) as mocked:
            code, out = self.run_main(["run", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertIn(
            "Work folder: active (the only ongoing and runnable one, "
            "selected automatically)", out)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "active")

    def test_run_without_folder_excludes_waiting_folder(self):
        config = self.write_config()
        self.write_task("base", "BLOCKED")
        self.write_task("waiting", "TODO")
        (config.parent / "waiting" / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        self.write_task("ready", "TODO")

        with patch("assent.__main__.engine.run", return_value=0) as mocked:
            code, out = self.run_main(["run", "--config", str(config)])

        self.assertEqual(code, 0)
        self.assertIn("Work folder: ready", out)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "ready")

    def test_run_without_folder_lists_waiting_reason(self):
        config = self.write_config()
        self.write_task("base", "BLOCKED")
        self.write_task("waiting", "TODO")
        (config.parent / "waiting" / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")

        with patch("assent.__main__.engine.run") as mocked:
            code, out = self.run_main(["run", "--config", str(config)])

        self.assertEqual(code, 1)
        self.assertIn("0 ongoing and runnable folder(s) found", out)
        self.assertIn("waiting:", out)
        self.assertIn("(waiting on base)", out)
        mocked.assert_not_called()

    def test_run_without_folder_refuses_zero_or_multiple_ongoing(self):
        for case, statuses in (("zero", [("archive", "DONE")]),
                               ("multiple", [("one", "TODO"),
                                             ("two", "WIP")])):
            with self.subTest(case=case):
                shutil.rmtree(self.root / ".assent", ignore_errors=True)
                config = self.write_config()
                for folder, status in statuses:
                    self.write_task(folder, status)
                with patch("assent.__main__.engine.run") as mocked:
                    code, out = self.run_main(
                        ["run", "--config", str(config)])
                self.assertEqual(code, 1)
                self.assertIn("State the work folder explicitly", out)
                for folder, _ in statuses:
                    self.assertIn(folder, out)
                mocked.assert_not_called()

    def test_run_without_folder_refuses_when_no_task_folder_exists(self):
        config = self.write_config()
        code, out = self.run_main(["run", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("no work folder with a task file found", out)

    def test_read_only_commands_without_folder_run_all_folders(self):
        config = self.write_config()
        self.write_task("beta")
        self.write_task("alpha")
        for command in ("status", "check", "report"):
            with self.subTest(command=command), patch(
                    f"assent.__main__.inspection.{command}", return_value=0) as mocked:
                code, _ = self.run_main([command, "--config", str(config)])
                self.assertEqual(code, 0)
                self.assertEqual(
                    [call.args[0].tasks_name for call in mocked.call_args_list],
                    ["alpha", "beta"])

    def test_check_without_folder_fails_if_any_folder_fails(self):
        config = self.write_config()
        self.write_task("alpha")
        self.write_task("beta")
        with patch("assent.__main__.inspection.check", side_effect=[0, 1]) as mocked:
            code, _ = self.run_main(["check", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual(mocked.call_count, 2)

    def test_check_rejects_selection_before_any_plan_check_runs(self):
        config = self.write_config(
            '[abilities.review]\n'
            'prompt = "Review the selection."\n'
            'writes = false\n'
            'produces_verdict = true\n'
            '[roles.reviewer]\n'
            'ability = ["review"]\n'
            'model = "prime"\n'
            'effort = "heavy"\n'
            '[workflow]\nselection = ['
            '{ role = "reviewer", adapter = "codex" }, '
            '{ action = "full_verify" }]\n')
        self.write_task("alpha")

        with patch("assent.__main__.inspection.check") as mocked:
            code, out = self.run_main(["check", "--config", str(config)])

        self.assertEqual(code, 1)
        self.assertIn("unknown keys: selection", out)
        mocked.assert_not_called()

    def test_check_without_folder_rejects_bad_folder_graph(self):
        cases = {
            "bad-format": ('after = [\n',),
            "missing-reference": ('after = ["missing"]\n',),
            "cycle": ('after = ["beta"]\n', 'after = ["alpha"]\n'),
        }
        for name, declarations in cases.items():
            with self.subTest(name=name):
                shutil.rmtree(self.root / ".assent", ignore_errors=True)
                config = self.write_config()
                self.write_task("alpha")
                (config.parent / "alpha" / "_folder.toml").write_text(
                    declarations[0], encoding="utf-8")
                if len(declarations) == 2:
                    self.write_task("beta")
                    (config.parent / "beta" / "_folder.toml").write_text(
                        declarations[1], encoding="utf-8")
                with patch("assent.__main__.inspection.check", return_value=0):
                    code, out = self.run_main(
                        ["check", "--config", str(config)])
                self.assertEqual(code, 1)
                self.assertIn("Folder dependency graph: FAIL", out)


class TestAutomaticIntegrationChaining(MainTestCase):
    """A successful run automatically enters its matching integration workflow."""

    def setUp(self):
        super().setUp()
        self.config = self.write_config(
            '[workflow]\nintegration = [{ action = "full_verify" }]\n')
        self.assent_dir = self.config.parent.resolve()

    def test_one_completed_folder_runs_exact_integration(self):
        self.write_task("alpha", "DONE")
        with patch("assent.__main__.engine.run", return_value=0), \
                patch("assent.__main__.engine.run_selection_workflow",
                      return_value=0) as integration:
            code, _ = self.run_main([
                "run", "alpha", "--config", str(self.config)])
        self.assertEqual(code, 0)
        integration.assert_called_once_with(
            str(self.config), self.assent_dir, ["alpha"])

    def test_exact_selection_runs_one_integration_workflow(self):
        self.write_task("alpha", "DONE")
        self.write_task("beta", "DONE")
        with patch("assent.__main__.engine.run", return_value=0), \
                patch("assent.__main__.engine.run_selection_workflow",
                      return_value=0) as integration:
            code, _ = self.run_main([
                "run", "alpha", "beta", "--config", str(self.config)])
        self.assertEqual(code, 0)
        integration.assert_called_once_with(
            str(self.config), self.assent_dir, ["alpha", "beta"])

    def test_all_runs_dynamic_integration_after_success(self):
        self.write_task("alpha", "DONE")
        with patch("assent.__main__.run_all", return_value=0), \
                patch("assent.__main__.engine.run_dynamic_selection_workflow",
                      return_value=0) as integration:
            code, _ = self.run_main([
                "run", "--all", "--config", str(self.config)])
        self.assertEqual(code, 0)
        integration.assert_called_once_with(str(self.config), self.assent_dir)

    def test_failed_run_starts_no_integration(self):
        self.write_task("alpha")
        with patch("assent.__main__.engine.run", return_value=3), \
                patch("assent.__main__.engine.run_selection_workflow") as exact, \
                patch("assent.__main__.engine.run_dynamic_selection_workflow") as dynamic:
            code, _ = self.run_main([
                "run", "alpha", "--config", str(self.config)])
        self.assertEqual(code, 3)
        exact.assert_not_called()
        dynamic.assert_not_called()

    def test_limited_incomplete_run_defers_integration(self):
        self.write_task("alpha", "TODO")
        with patch("assent.__main__.engine.run", return_value=0), \
                patch("assent.__main__.engine.run_selection_workflow") as integration:
            code, out = self.run_main([
                "run", "alpha", "--once", "--config", str(self.config)])
        self.assertEqual(code, 0)
        self.assertIn("Integration workflow deferred", out)
        integration.assert_not_called()

class TestCommandElapsed(MainTestCase):
    """`run` and `verify` report their end-to-end wall-clock duration.

    The clock is injected, so every assertion is on the reported arithmetic
    rather than on how long the test machine happened to take.
    """

    # Two readings per invocation, 2.5 seconds apart, whatever the machine does.
    def injected_clock(self):
        return patch("assent.__main__._monotonic",
                     side_effect=itertools.count(0.0, 2.5))

    def total_lines(self, output: str) -> list[str]:
        return [line for line in output.splitlines()
                if line.startswith("Command `assent ")]

    def test_every_run_path_reports_one_total_with_the_unchanged_exit_code(self):
        config = self.write_config()
        for folder in ("alpha", "beta"):
            self.write_task(folder)
        cases = {
            "direct": ["run", "alpha"],
            "selected": ["run", "alpha", "beta"],
            "remainder": ["run", "alpha", "..."],
            "automatic": ["run"],
        }
        for name, argv in cases.items():
            for result in (0, 1):
                with self.subTest(case=name, result=result):
                    self.write_task("alpha")
                    # The automatic selection needs exactly one ongoing folder.
                    self.write_task(
                        "beta", "DONE" if name == "automatic" else "TODO")
                    with self.injected_clock(), patch(
                            "assent.__main__.engine.run", return_value=result):
                        code, out = self.run_main(
                            argv + ["--config", str(config)])
                    self.assertEqual(code, result)
                    self.assertEqual(
                        self.total_lines(out),
                        [f"Command `assent run` finished: elapsed 2.5s, "
                         f"exit code {result}"])

        with self.injected_clock(), patch("assent.__main__.run_all",
                                          return_value=0), patch(
                "assent.__main__.verify_batch",
                return_value=0):
            code, out = self.run_main(["run", "--all", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.total_lines(out),
            ["Command `assent run` finished: elapsed 2.5s, exit code 0"])

    def test_a_scheduler_child_labels_its_folder_total_apart(self):
        config = self.write_config()
        self.write_task("work")
        os.environ["ASSENT_FOLDER_RUN"] = "1"
        with self.injected_clock(), patch("assent.__main__.engine.run",
                                          return_value=0):
            code, out = self.run_main(["run", "work", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(self.total_lines(out), [])
        self.assertIn(
            "Scheduled folder run finished: elapsed 2.5s, exit code 0", out)

    def test_verification_paths_keep_the_verifier_only_elapsed_line(self):
        config = self.write_config()
        for folder in ("earlier", "later"):
            self.write_task(folder, "DONE")

        def verifier(*args, **kwargs):
            # What the full verifier itself reports, from inside the command.
            print("Full verification finished: elapsed 1.0s, exit code 0")
            return 0

        cases = {
            "folder": (["verify", "earlier"],
                       "assent.__main__.engine.run_selection_workflow"),
            "selected": (["verify", "earlier", "later"],
                         "assent.__main__.engine.run_selection_workflow"),
            "batch": (["verify", "--batch"],
                      "assent.__main__.engine.run_dynamic_selection_workflow"),
            "focused": (["verify", "earlier", "--focus"],
                         "assent.__main__.engine.verify_focused"),
        }
        for name, (argv, target) in cases.items():
            with self.subTest(case=name):
                with self.injected_clock(), patch(target, side_effect=verifier):
                    code, out = self.run_main(argv + ["--config", str(config)])
                self.assertEqual(code, 0)
                self.assertEqual(
                    self.total_lines(out),
                    ["Command `assent verify` finished: elapsed 2.5s, "
                     "exit code 0"])
                self.assertIn(
                    "Full verification finished: elapsed 1.0s, exit code 0",
                    out)

        # `run --verify` is one invocation covering both stages, so it reports
        # one run-shaped total, not a second verify one.
        self.write_task("earlier", "DONE")
        with self.injected_clock(), \
                patch("assent.__main__.engine.run", return_value=0), \
                patch("assent.__main__.verify_folder_if_needed",
                      side_effect=verifier):
            code, out = self.run_main(
                ["run", "earlier", "--verify", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(
            self.total_lines(out),
            ["Command `assent run` finished: elapsed 2.5s, exit code 0"])
        self.assertIn("Full verification finished: elapsed 1.0s, exit code 0",
                      out)

    def test_refusal_and_interrupt_keep_their_result_and_report_elapsed(self):
        config = self.write_config()
        self.write_task("reviewed", "DONE")

        # A configuration refusal never reaches an engine or a verifier.
        with self.injected_clock():
            code, out = self.run_main(["verify", "reviewed", "--config",
                                       str(self.root / "absent.toml")])
        self.assertEqual(code, 1)
        self.assertIn("Config error:", out)
        self.assertEqual(
            self.total_lines(out),
            ["Command `assent verify` finished: elapsed 2.5s, exit code 1"])

        # A handled Ctrl+C keeps its own diagnostic and its 130.
        with self.injected_clock(), patch(
                "assent.__main__.engine.run_selection_workflow",
                side_effect=KeyboardInterrupt):
            code, out = self.run_main(
                ["verify", "reviewed", "--config", str(config)])
        self.assertEqual(code, 130)
        self.assertIn("temporary resources were cleaned up", out)
        self.assertEqual(
            self.total_lines(out),
            ["Command `assent verify` finished: elapsed 2.5s, exit code 130"])

        # An interrupt that leaves the command is timed and then re-raised, so
        # the caller's own interrupt handling is unchanged.
        self.write_task("work")
        out = io.StringIO()
        with self.injected_clock(), patch("assent.__main__.engine.run",
                                          side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt), \
                    contextlib.redirect_stdout(out):
                main(["run", "work", "--config", str(config)])
        self.assertEqual(
            self.total_lines(out.getvalue()),
            ["Command `assent run` interrupted: elapsed 2.5s, exit code 130"])

    def test_the_total_is_retained_by_the_ordinary_terminal_log(self):
        config = self.write_config()
        self.write_task("reviewed", "DONE")
        with self.injected_clock(), patch(
                "assent.__main__.engine.run_selection_workflow",
                return_value=0):
            code, _ = self.run_main(
                ["verify", "reviewed", "--config", str(config)])
        self.assertEqual(code, 0)
        log = (self.root / ".assent" / "reviewed" / "_assent.log").read_text(
            encoding="utf-8")
        self.assertIn(
            "Command `assent verify` finished: elapsed 2.5s, exit code 0", log)

    def test_the_other_commands_report_no_elapsed_time(self):
        config = self.write_config()
        self.write_task("done", "DONE")
        cases = {
            "status": (["status"], None),
            "check": (["check"], None),
            "report": (["report"], None),
            "accept": (["accept", "done"], "assent.__main__.accept_folder"),
            "clean": (["clean", "done"], "assent.__main__.clean_folders"),
            "archive": (["archive", "done"], "assent.__main__.archive_folder"),
            "reconcile": (["reconcile", "done"],
                          "assent.__main__.reconcile_start"),
            "reject": (["reject", "done"], "assent.__main__.reject_folder"),
            "rework": (["rework", "done", "t001"],
                       "assent.__main__.rework_task"),
            "doctor": (["doctor"], "assent.__main__.run_doctor"),
        }
        for name, (argv, target) in cases.items():
            with self.subTest(command=name):
                patched = (patch(target, return_value=0) if target
                           else contextlib.nullcontext())
                with self.injected_clock(), patched:
                    _code, out = self.run_main(
                        argv + ([] if name == "doctor"
                                else ["--config", str(config)]))
                self.assertNotIn("elapsed", out)


class TestHelpPalette(MainTestCase):
    """Help stays colored on Python 3.14+ but never dark blue, and stays
    plain whenever the standard controls disable color."""

    def help_output(self, argv, environment) -> str:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_PROJECT_ROOT)
        for name in ("FORCE_COLOR", "NO_COLOR", "PYTHON_COLORS", "TERM"):
            env.pop(name, None)
        env.update(environment)
        result = subprocess.run(
            [sys.executable, "-m", "assent", *argv, "--help"],
            cwd=self.root, capture_output=True, text=True,
            encoding="utf-8", env=env)
        self.assertEqual(result.returncode, 0)
        return result.stdout

    def test_forced_color_uses_bright_cyan_headings_and_no_dark_blue(self):
        for argv in ([], ["run"], ["accept"], ["verify"]):
            with self.subTest(argv=argv):
                text = self.help_output(argv, {"FORCE_COLOR": "1"})
                self.assertNotIn("\x1b[1;34m", text)
                if sys.version_info >= (3, 14):
                    # Only 3.14+ argparse colorizes help at all; older versions
                    # legitimately emit no escapes even when color is forced.
                    self.assertIn("\x1b[1;96musage: ", text)
                    self.assertIn("\x1b[1;96moptions:", text)
                    # Unrelated colors keep the standard theme.
                    self.assertIn("\x1b[1;35m", text)
                else:
                    self.assertNotIn("\x1b[", text)

    def test_disabled_color_emits_no_escape_sequences(self):
        for environment in ({"NO_COLOR": "1"}, {"PYTHON_COLORS": "0"},
                            {"TERM": "dumb"}, {}):
            for argv in ([], ["run"], ["accept"]):
                with self.subTest(environment=environment, argv=argv):
                    self.assertNotIn(
                        "\x1b[", self.help_output(argv, environment))

    def test_plan_arguments_name_the_domain_concept_and_explain_storage(self):
        for command in ("run", "status", "check", "report", "verify", "clean",
                        "archive", "accept", "reconcile", "reject", "rework"):
            with self.subTest(command=command):
                help_text = " ".join(
                    self.help_output([command], {}).split())
                self.assertIn("PLAN", help_text)
                self.assertNotIn("FOLDER", help_text)
                self.assertIn("Each PLAN names a directory directly under the "
                              "project's `.assent/`", help_text)
                self.assertIn("pass the name, not a path", help_text)

    def test_help_states_the_remainder_syntax_and_the_two_receipt_paths(self):
        run_help = " ".join(self.help_output(["run"], {}).split())
        self.assertIn("the literal token `...` as the last argument adds every "
                      "remaining discovered plan", run_help)
        self.assertIn("Each PLAN names a directory directly under the project's "
                      "`.assent/`", run_help)
        self.assertIn("pass the name, not a path", run_help)
        self.assertIn("After the whole run exits zero, run the complete "
                      "verification that matches the selection", run_help)
        self.assertIn("With --once or --task it verifies only when that limited "
                      "run left the single selected plan complete, and an "
                      "incomplete plan fails the request without writing a "
                      "receipt", run_help)
        self.assertNotIn("cannot be used with --once or --task", run_help)

        accept_help = " ".join(self.help_output(["accept"], {}).split())
        self.assertIn("a fresh PASSED batch receipt is replayed and released "
                      "atomically without new verification", accept_help)
        self.assertIn("absent or expired batch evidence verifies each "
                      "not-yet-integrated plan in turn", accept_help)
        self.assertNotIn("(sequential only)", accept_help)


class TestRemainderSelection(MainTestCase):
    """The literal `...` token as a remainder selector across the workflow.

    `A B ...` is A and B plus the folders that command would otherwise discover
    for the whole project.  These tests patch the operation each branch calls,
    so they prove what the CLI selects rather than re-testing the operations.
    """

    def usage_error(self, argv) -> str:
        errors = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(
                errors), contextlib.redirect_stdout(io.StringIO()):
            main(argv)
        self.assertEqual(ctx.exception.code, 2)
        return errors.getvalue()

    def test_marker_is_rejected_when_repeated_misplaced_or_incompatible(self):
        cases = (
            ["run", "...", "..."],
            ["run", "...", "first"],
            ["run", "first", "...", "--all"],
            ["run", "first", "...", "--once"],
            ["run", "first", "...", "--task", "t001"],
            ["accept", "first", "...", "--all"],
            ["accept", "...", "first"],
            ["verify", "first", "...", "--batch"],
            ["verify", "first", "...", "--focus"],
            ["verify", "...", "..."],
            ["clean", "...", "first"],
            ["clean", "first", "first"],
            ["archive", "first", "...", "--all"],
            ["archive", "first", "...", "--restore"],
            ["archive", "...", "--restore"],
            ["archive", "first", "second", "--restore"],
            ["archive", "first", "first"],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                self.usage_error(argv)

    def test_marker_never_reaches_configuration_loading_as_a_folder_name(self):
        config = self.write_config()
        self.write_task("alpha", "DONE")
        real_load = load_config
        seen: list[str] = []

        def record(path, folder):
            seen.append(folder)
            return real_load(path, folder)

        commands = (["run", "..."], ["clean", "..."], ["archive", "..."],
                    ["verify", "..."], ["accept", "..."])
        for argv in commands:
            with self.subTest(argv=argv), \
                    patch("assent.__main__.load_config", side_effect=record), \
                    patch("assent.__main__.engine.run", return_value=0), \
                    patch("assent.__main__.engine.run_selection_workflow",
                          return_value=0), \
                    patch("assent.__main__.verify_batch", return_value=0), \
                    patch("assent.__main__.clean_folders", return_value=0), \
                    patch("assent.__main__.archive_folder", return_value=0), \
                    patch("assent.__main__.accept_folder", return_value=0):
                code, _ = self.run_main([*argv, "--config", str(config)])
                self.assertEqual(code, 0)
        self.assertEqual(set(seen), {"alpha"})

    def test_run_completes_the_explicit_prefix_before_the_remainder(self):
        config = self.write_config()
        for folder in ("alpha", "beta", "delta", "gamma"):
            self.write_task(folder)
        # delta must wait for beta, so the remainder is dependency-ordered
        # rather than merely lexicographic.
        (self.root / ".assent" / "delta" / "_folder.toml").write_text(
            'after = ["beta"]\n', encoding="utf-8")
        order: list[str] = []
        with patch("assent.__main__.engine.run",
                   side_effect=lambda cfg, **_: order.append(cfg.tasks_name)
                   or 0), \
                patch("assent.__main__.run_all",
                      side_effect=AssertionError("used the --all scheduler")):
            code, out = self.run_main(
                ["run", "gamma", "alpha", "...", "--config", str(config)])

        self.assertEqual(code, 0)
        self.assertIn("run: `...` selects gamma, alpha, beta, delta", out)
        self.assertEqual(order, ["gamma", "alpha", "beta", "delta"])

    def test_run_explicit_prefix_failure_prevents_remainder_scheduling(self):
        config = self.write_config()
        for folder in ("alpha", "beta"):
            self.write_task(folder)
        with patch("assent.__main__.engine.run", return_value=1) as engine_run, \
                patch("assent.__main__.run_all",
                      side_effect=AssertionError("remainder was scheduled")):
            code, _ = self.run_main(
                ["run", "alpha", "...", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual([call.args[0].tasks_name
                          for call in engine_run.call_args_list], ["alpha"])

    def test_run_remainder_is_a_snapshot_taken_before_the_prefix_runs(self):
        config = self.write_config()
        self.write_task("alpha")
        started: list[str] = []

        def create_a_folder_mid_run(cfg, **_):
            started.append(cfg.tasks_name)
            self.write_task("appeared")
            return 0

        with patch("assent.__main__.engine.run",
                   side_effect=create_a_folder_mid_run):
            code, out = self.run_main(
                ["run", "alpha", "...", "--config", str(config)])

        self.assertEqual(code, 0)
        self.assertIn("No remaining work folder to run.", out)
        self.assertEqual(started, ["alpha"])

    def test_verify_remainder_expands_to_finished_folders_only(self):
        config = self.write_config()
        self.write_task("alpha", "DONE")
        self.write_task("beta", "DONE")
        self.write_task("ongoing", "TODO")
        with patch("assent.__main__.engine.run_selection_workflow",
                   return_value=0) as batch:
            code, out = self.run_main(
                ["verify", "beta", "...", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(batch.call_args.args[2], ["beta", "alpha"])
        self.assertNotIn("ongoing", out)

    def test_verify_remainder_of_one_folder_uses_the_folder_path(self):
        config = self.write_config()
        self.write_task("alpha", "DONE")
        self.write_task("ongoing", "TODO")
        with patch("assent.__main__.engine.run_selection_workflow",
                   return_value=0) as folder:
            code, _ = self.run_main(["verify", "...", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(folder.call_args.args[2], ["alpha"])

        # A one-folder expansion is not a batch, so --no-bisect is a usage error
        # exactly as it is for a single named folder.
        self.usage_error(["verify", "...", "--no-bisect", "--config",
                          str(config)])

    def test_accept_remainder_never_falls_back_to_accept_all(self):
        config = self.write_config()
        self.write_task("alpha", "DONE")
        self.write_task("beta", "DONE")
        self.write_task("ongoing", "TODO")
        with patch("assent.__main__.accept_selected_batch",
                   return_value=0) as batch, \
                patch("assent.__main__.accept_all",
                      side_effect=AssertionError("fell back to accept --all")), \
                patch("assent.__main__.verify_folder",
                      side_effect=AssertionError("accept verified")):
            code, _ = self.run_main(
                ["accept", "beta", "...", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(batch.call_args.args[2], ["beta", "alpha"])

    def test_accept_remainder_of_one_folder_uses_the_direct_gate(self):
        config = self.write_config()
        self.write_task("alpha", "DONE")
        self.write_task("ongoing", "TODO")
        with patch("assent.__main__.accept_folder", return_value=0) as direct, \
                patch("assent.__main__.accept_selected_batch",
                      side_effect=AssertionError("used the batch path")):
            code, _ = self.run_main(["accept", "...", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(direct.call_args.args[0].tasks_name, "alpha")

    def test_clean_accepts_an_exact_set_and_a_remainder(self):
        config = self.write_config()
        for folder in ("alpha", "beta", "gamma"):
            self.write_task(folder)
        with patch("assent.__main__.clean_folders", return_value=0) as mocked:
            code, _ = self.run_main(
                ["clean", "gamma", "alpha", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual([cfg.tasks_name for cfg in mocked.call_args.args[0]],
                         ["gamma", "alpha"])

        with patch("assent.__main__.clean_folders", return_value=1) as mocked:
            code, _ = self.run_main(
                ["clean", "gamma", "...", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual([cfg.tasks_name for cfg in mocked.call_args.args[0]],
                         ["gamma", "alpha", "beta"])

    def test_archive_selection_and_remainder_use_the_explicit_policy(self):
        config = self.write_config()
        for folder in ("alpha", "beta", "gamma"):
            self.write_task(folder, "DONE")
        with patch("assent.__main__.archive_selected", return_value=1) as mocked, \
                patch("assent.__main__.archive_all",
                      side_effect=AssertionError("used the --all policy")):
            code, _ = self.run_main(
                ["archive", "gamma", "...", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual(mocked.call_args.args[1], ["gamma", "alpha", "beta"])

        with patch("assent.__main__.archive_folder", return_value=0) as one:
            code, _ = self.run_main(
                ["archive", "beta", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(one.call_args.args[0].tasks_name, "beta")

    def test_archive_restore_stays_a_single_folder_operation(self):
        config = self.write_config()
        self.write_task("alpha", "DONE")
        with patch("assent.__main__.restore_folder", return_value=0) as mocked:
            code, _ = self.run_main(
                ["archive", "alpha", "--restore", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "alpha")

    def test_remainder_without_any_selectable_folder_refuses(self):
        config = self.write_config()
        commands = ("run", "clean", "verify", "accept", "archive")
        for command in commands:
            with self.subTest(command=command, project="empty"):
                code, out = self.run_main([command, "...", "--config",
                                           str(config)])
                self.assertEqual(code, 1)
                self.assertIn(f"{command}: `...` selected no work folder.", out)

        # verify and accept only ever select finished folders, so an entirely
        # unfinished project leaves their remainder empty too.
        self.write_task("ongoing", "TODO")
        for command in ("verify", "accept"):
            with self.subTest(command=command, project="unfinished"):
                code, out = self.run_main([command, "...", "--config",
                                           str(config)])
                self.assertEqual(code, 1)
                self.assertIn(f"{command}: `...` selected no work folder.", out)


class TestStdinStopWatcher(unittest.TestCase):
    """The stdin stop channel is opt-in: only a scheduler-spawned child gets
    ASSENT_STDIN_STOP, so a hand-typed `assent run` keeps its stdin."""

    _GIT_PROBE = """
import sys
from pathlib import Path
from assent.__main__ import _start_stdin_stop_watcher
from assent.gitops import _run_git

_start_stdin_stop_watcher()
result = _run_git(Path.cwd(), "ls-files", "--", ".assent")
print(result.returncode, flush=True)
"""

    def test_no_watcher_thread_without_the_environment_variable(self):
        environment = dict(os.environ)
        environment.pop("ASSENT_STDIN_STOP", None)
        before = threading.active_count()
        with patch.dict(os.environ, environment, clear=True):
            self.assertIsNone(_start_stdin_stop_watcher())
        self.assertEqual(threading.active_count(), before)

    def test_stdin_eof_calls_interrupt_main(self):
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        self.addCleanup(reader.close)

        class FakeStdin:
            buffer = reader

        with patch.dict(os.environ, {"ASSENT_STDIN_STOP": "1"}), patch.object(
                sys, "stdin", FakeStdin()), patch(
                "assent.__main__._thread.interrupt_main") as interrupt:
            thread = _start_stdin_stop_watcher()
            self.assertIsNotNone(thread)
            os.close(write_fd)  # what the parent closing the pipe looks like
            thread.join(timeout=10)

        self.assertFalse(thread.is_alive())
        interrupt.assert_called_once_with()

    def test_stdin_eof_wakes_blocking_waits_after_marking_the_interrupt(self):
        """interrupt_main() only makes the exception pending, so the watcher
        must also release whatever wait the main thread is parked in -- and in
        that order, so the wait is never woken before there is anything to
        deliver."""
        read_fd, write_fd = os.pipe()
        reader = os.fdopen(read_fd, "rb", buffering=0)
        self.addCleanup(reader.close)
        self.addCleanup(clear_stop_wake)

        class FakeStdin:
            buffer = reader

        order: list[str] = []
        with patch.dict(os.environ, {"ASSENT_STDIN_STOP": "1"}), patch.object(
                sys, "stdin", FakeStdin()), patch(
                "assent.__main__._thread.interrupt_main",
                side_effect=lambda: order.append("interrupt")), patch(
                "assent.__main__.wake_stop_waiters",
                side_effect=lambda: order.append("wake")):
            thread = _start_stdin_stop_watcher()
            os.close(write_fd)
            thread.join(timeout=10)

        self.assertEqual(order, ["interrupt", "wake"])

    @unittest.skipUnless(os.name == "nt", "stdin pipe inheritance hang is Windows-only")
    def test_watcher_pipe_is_not_inherited_by_git_subprocess(self):
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(_PROJECT_ROOT)
        environment["ASSENT_STDIN_STOP"] = "1"
        process = subprocess.Popen(
            [sys.executable, "-c", self._GIT_PROBE],
            cwd=_PROJECT_ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        timed_out = False
        try:
            try:
                returncode = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.stdin.close()
                returncode = process.wait(timeout=10)
            output = process.stdout.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()

        self.assertFalse(timed_out, "Git inherited the stop pipe and hung")
        self.assertEqual(returncode, 0, output)
        self.assertEqual(output.strip(), "0")


class TestInit(MainTestCase):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)

    def test_creates_skeleton(self):
        code, out = self.run_main(["init", "--test", "unittest"])
        self.assertEqual(code, 0)
        for rel in (".assent/verify.py", "AGENTS.md", ".gitignore"):
            self.assertTrue((self.root / rel).is_file(), rel)
        # Settings and contracts are the user home's; a fresh project carries
        # no copy of either and works from the shared ones.
        for rel in (".assent/assent.toml", ".assent/instructions.md",
                    ".assent/format.md"):
            self.assertFalse((self.root / rel).exists(), rel)
        for name in ("assent.toml", "instructions.md", "format.md"):
            self.assertTrue((self.user_home / name).is_file(), name)
        # Work folders are not pre-created: their name is decided by a
        # planning meeting based on the task, so pre-creating one would mislead.
        subdirs = [p for p in (self.root / ".assent").iterdir() if p.is_dir()]
        self.assertEqual(subdirs, [])
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        lines = gitignore.splitlines()
        self.assertIn(".assent/", lines)
        self.assertNotIn("AGENTS.md", lines)
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md.count("<!-- assent-instructions -->"), 1)
        self.assertIn("`~/.assent/instructions.md`", agents_md)
        self.assertIn("An AI session never initiates the full suite", agents_md)
        self.assertIn("the scheduler owns workflow `full_verify`", agents_md)
        config = (self.user_home / "assent.toml").read_text(encoding="utf-8")
        self.assertNotIn("assent-config-schema", config)
        self.assertNotIn("[git]", config)
        self.assertNotIn("[plan]", config)
        adapter = (self.user_home / "adapter.toml").read_text(encoding="utf-8")
        self.assertNotIn("assent-adapter-schema", adapter)
        verifier = (self.root / ".assent" / "verify.py").read_text(
            encoding="utf-8")
        self.assertNotIn("assent-verifier-template", verifier)
        self.assertIn("\nrun_unittest_parallel()\n", verifier)
        self.assertIn("\n# run(\"pytest\")\n", verifier)
        self.assertIn("\n# run(\"npm\", \"test\")\n", verifier)
        self.assertIn("\n# run(\"flutter\", \"test\")\n", verifier)
        self.assertIn("\n# run(\"dotnet\", \"test\")\n", verifier)
        self.assertIn("\n# run(\"mvn\", \"test\")\n", verifier)
        self.assertIn("\n# run(\"gradle\", \"test\")\n", verifier)
        self.assertIn(
            "\n# run(\"ctest\", \"--test-dir\", \"build\", "
            "\"--output-on-failure\")\n", verifier)
        self.assertIn("\n# run(\"make\", \"test\")\n", verifier)
        self.assertIn("Created:", out)

    def test_builtin_test_choices_activate_only_the_selected_command(self):
        choices = {
            "unittest": "run_unittest_parallel()",
            "pytest": 'run("pytest")',
            "npm": 'run("npm", "test")',
            "flutter": 'run("flutter", "test")',
            "dotnet": 'run("dotnet", "test")',
            "maven": 'run("mvn", "test")',
            "gradle": 'run("gradle", "test")',
            "cmake-ctest":
                'run("ctest", "--test-dir", "build", "--output-on-failure")',
            "make": 'run("make", "test")',
        }
        for choice, active in choices.items():
            with self.subTest(choice=choice):
                root = self.root / choice
                root.mkdir()
                subprocess.run(["git", "init"], cwd=root, check=True,
                               capture_output=True)
                self.assertEqual(run_init(root, test=choice), 0)
                verifier = (root / ".assent" / "verify.py").read_text(
                    encoding="utf-8")
                self.assertIn(f"\n{active}\n", verifier)
                for other in choices.values():
                    if other != active:
                        self.assertIn(f"\n# {other}\n", verifier)

    def test_cli_without_test_shows_menu_and_uses_the_selected_choice(self):
        with patch("builtins.input", return_value="2"):
            code, output = self.run_main(["init"])
        self.assertEqual(code, 0)
        self.assertIn("0. Custom command", output)
        self.assertIn("1. Parallel unittest", output)
        verifier = (self.root / ".assent" / "verify.py").read_text(
            encoding="utf-8")
        self.assertIn('\nrun("pytest")\n', verifier)

    def test_custom_test_choice_is_quoted_as_argv(self):
        self.assertEqual(
            run_init(self.root, test=["custom", "python", "-m", "unittest",
                                      "tests/special case"]), 0)
        verifier = (self.root / ".assent" / "verify.py").read_text(
            encoding="utf-8")
        self.assertIn(
            'run("python", "-m", "unittest", "tests/special case")', verifier)
        self.assertNotIn("os.system", verifier)

    def test_invalid_selection_and_eof_leave_a_fresh_project_untouched(self):
        invalid_out = io.StringIO()
        with contextlib.redirect_stdout(invalid_out):
            self.assertEqual(run_init(self.root, test="not-a-choice"), 1)
        self.assertIn("Refused:", invalid_out.getvalue())
        self.assertFalse((self.root / ".assent").exists())

        with patch("builtins.input", side_effect=EOFError), contextlib.redirect_stdout(
                io.StringIO()):
            self.assertEqual(run_init(self.root, test=None), 1)
        self.assertFalse((self.root / ".assent").exists())

    def test_empty_git_project_verifier_fails_until_tests_exist(self):
        self.assertEqual(run_init(self.root, test="unittest"), 0)
        result = subprocess.run(
            [sys.executable, ".assent/verify.py"], cwd=self.root,
            capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("verify: OK", result.stdout)

    def test_idempotent_no_overwrite_no_duplicates(self):
        run_init(self.root, test="unittest")
        user_config = self.user_home / "assent.toml"
        user_config.write_text(
            user_config.read_text(encoding="utf-8").replace(
                "retry_per_task = 1",
                'retry_per_task = 7\ncustom_setting = "keep"'),
            encoding="utf-8")
        (self.user_home / "instructions.md").write_text(
            "an older assent's working instructions\n", encoding="utf-8")
        (self.user_home / "format.md").write_text(
            "old format\n", encoding="utf-8")
        verifier_before = (self.root / ".assent" / "verify.py").read_bytes()
        out = io.StringIO()
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root), 0)
        config_text = user_config.read_text(encoding="utf-8")
        config = tomllib.loads(config_text)
        self.assertEqual(config["run"]["retry_per_task"], 7)
        self.assertEqual(config["run"]["custom_setting"], "keep")
        self.assertIn("quota_poll_minutes", config["run"])
        self.assertIn("watchdog", config)
        loaded = load_config(self.root / ".assent" / "assent.toml", "plan01")
        task_steps = loaded.workflow_task
        self.assertIsNotNone(task_steps)
        self.assertEqual(
            [step.action if hasattr(step, "action") else "role"
             for step in task_steps],
            ["role", "focused_test", "role", "focused_test"])
        task_roles = [step for step in task_steps
                      if not hasattr(step, "action")]
        self.assertEqual(
            [(step.writes, step.produces_verdict) for step in task_roles],
            [(True, False), (True, True)])
        self.assertEqual(config["workflow"]["integration"][0],
                         {"action": "full_verify"})
        self.assertEqual(config["workflow"]["integration"][-1],
                         {"action": "full_verify"})
        config_after_first_upgrade = user_config.read_bytes()
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root), 0)
        self.assertEqual(user_config.read_bytes(), config_after_first_upgrade)
        self.assertEqual(
            (self.user_home / "format.md").read_text(encoding="utf-8"),
            (_PROJECT_ROOT / "assent/templates/format.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.user_home / "instructions.md").read_text(encoding="utf-8"),
            (_PROJECT_ROOT / "assent/templates/instructions.md").read_text(encoding="utf-8"))
        self.assertEqual(verifier_before,
                         (self.root / ".assent" / "verify.py").read_bytes())
        self.assertIn("Updated:", out.getvalue())
        self.assertIn("Preserved:", out.getvalue())
        # gitignore entries are not duplicated on re-run.
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gitignore.splitlines().count(".assent/"), 1)
        # The managed contracts refresh, while the AGENTS.md bridge is not duplicated.
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md.count("<!-- assent-instructions -->"), 1)

    def test_init_does_not_overwrite_existing_verifier(self):
        run_init(self.root, test="unittest")
        verifier = self.root / ".assent" / "verify.py"
        custom = "# project-specific verifier\n"
        verifier.write_text(custom, encoding="utf-8")
        output = io.StringIO()
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)
        self.assertEqual(verifier.read_text(encoding="utf-8"), custom)
        self.assertIn("replacement declined", output.getvalue())

    def test_test_option_backs_up_and_replaces_existing_verifier(self):
        run_init(self.root, test="unittest")
        verifier = self.root / ".assent" / "verify.py"
        before = {path: path.read_bytes() for path in (
            verifier, self.user_home / "format.md",
            self.user_home / "instructions.md",
            self.user_home / "assent.toml")}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root, test="pytest"), 0)
        backup = verifier.with_name("verify.py.before-assent-replace")
        self.assertEqual(backup.read_bytes(), before[verifier])
        self.assertIn('\nrun("pytest")\n', verifier.read_text(encoding="utf-8"))
        self.assertIn("Backed up:", output.getvalue())
        for path, content in before.items():
            if path != verifier:
                self.assertEqual(path.read_bytes(), content)

    def test_differing_verifier_can_be_backed_up_and_replaced_from_menu(self):
        run_init(self.root, test="unittest")
        verifier = self.root / ".assent" / "verify.py"
        custom = b"# custom verifier\n"
        verifier.write_bytes(custom)

        output = io.StringIO()
        with patch("builtins.input", side_effect=("y", "2")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)

        backup = verifier.with_name("verify.py.before-assent-replace")
        self.assertEqual(backup.read_bytes(), custom)
        self.assertIn('\nrun("pytest")\n', verifier.read_text(encoding="utf-8"))
        self.assertIn("Choose the project's test command", output.getvalue())

    def test_invalid_existing_toml_does_not_partially_upgrade_managed_files(self):
        run_init(self.root, test="unittest")
        # A stale contract would be refreshed by a successful init; here the
        # unparsable user config must stop everything before the first write.
        (self.user_home / "format.md").write_text("old format\n", encoding="utf-8")
        (self.user_home / "assent.toml").write_text("[run\ninvalid", encoding="utf-8")
        managed = (
            self.root / ".assent/verify.py",
            self.user_home / "format.md",
            self.user_home / "instructions.md",
            self.user_home / "assent.toml",
        )
        before = {path: path.read_bytes() for path in managed}
        output = io.StringIO()
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 1)
        self.assertIn("not valid TOML", output.getvalue())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_differing_settings_are_prompted_and_replaced_independently(self):
        run_init(self.root, test="unittest")
        user_config = self.user_home / "assent.toml"
        user_adapter = self.user_home / "adapter.toml"
        user_config.write_text(
            user_config.read_text(encoding="utf-8").replace(
                "retry_per_task = 1", "retry_per_task = 7"),
            encoding="utf-8")
        user_adapter.write_text(
            user_adapter.read_text(encoding="utf-8").replace(
                'command = "codex"', 'command = "custom-codex"'),
            encoding="utf-8")
        config_before = user_config.read_bytes()
        adapter_before = user_adapter.read_bytes()

        output = io.StringIO()
        with patch("builtins.input", side_effect=("y", "n")) as questions, \
                contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)

        self.assertEqual(questions.call_count, 2)
        config_backup = user_config.with_name(
            "assent.toml.before-assent-replace")
        self.assertEqual(config_backup.read_bytes(), config_before)
        self.assertIn(
            "retry_per_task = 1", user_config.read_text(encoding="utf-8"))
        self.assertEqual(user_adapter.read_bytes(), adapter_before)
        self.assertFalse(user_adapter.with_name(
            "adapter.toml.before-assent-replace").exists())
        self.assertIn("Backed up:", output.getvalue())

        with patch("builtins.input", return_value="y") as questions, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root), 0)
        self.assertEqual(questions.call_count, 1)
        adapter_backup = user_adapter.with_name(
            "adapter.toml.before-assent-replace")
        self.assertEqual(adapter_backup.read_bytes(), adapter_before)
        self.assertIn(
            'command = "codex"', user_adapter.read_text(encoding="utf-8"))

    def test_effective_config_is_validated_before_contracts_are_refreshed(self):
        run_init(self.root, test="unittest")
        stale_contract = self.user_home / "format.md"
        stale_contract.write_text("stale contract\n", encoding="utf-8")
        user_config = self.user_home / "assent.toml"
        user_config.write_text(
            user_config.read_text(encoding="utf-8").replace(
                'task = [\n  { role = "implementer"},',
                'task = [\n  { action = "full_verify" },'),
            encoding="utf-8")

        output = io.StringIO()
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 1)

        self.assertEqual(
            stale_contract.read_text(encoding="utf-8"), "stale contract\n")
        self.assertIn("not valid under [workflow].task", output.getvalue())

    def test_two_projects_share_one_config_and_contract_set(self):
        second = self.root / "second-project"
        second.mkdir()
        subprocess.run(["git", "init"], cwd=second, check=True,
                       capture_output=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root, test="unittest"), 0)
        user_config = self.user_home / "assent.toml"
        user_config.write_text(
            user_config.read_text(encoding="utf-8").replace(
                "retry_per_task = 1", "retry_per_task = 5"),
            encoding="utf-8")
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(second, test="unittest"), 0)
        self.assertEqual(
            tomllib.loads(user_config.read_text(encoding="utf-8"))
            ["run"]["retry_per_task"], 5)
        for root in (self.root, second):
            for name in ("assent.toml", "instructions.md", "format.md"):
                self.assertFalse((root / ".assent" / name).exists(),
                                 f"{root}/{name}")
            self.assertTrue((root / ".assent/verify.py").is_file())

    def test_existing_project_config_is_preserved_byte_for_byte_with_a_warning(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run_init(self.root, test="unittest")
        project_config = self.root / ".assent" / "assent.toml"
        project_config.write_text(
            "[run]\nretry_per_task = 3   # local override\n", encoding="utf-8")
        before = project_config.read_bytes()
        output = io.StringIO()
        with patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)
        self.assertEqual(project_config.read_bytes(), before)
        text = output.getvalue()
        self.assertIn(f"Warning: {project_config}", text)
        self.assertIn("shadow the current shared settings", text)

    def test_project_override_can_be_backed_up_and_removed(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run_init(self.root, test="unittest")
        project_config = self.root / ".assent" / "assent.toml"
        content = b"[run]\nretry_per_task = 3\n"
        project_config.write_bytes(content)

        with patch("builtins.input", return_value="y"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root), 0)

        self.assertFalse(project_config.exists())
        self.assertEqual(
            project_config.with_name(
                "assent.toml.before-assent-replace").read_bytes(),
            content)

    def test_matching_local_contract_is_removed_and_a_differing_one_is_kept(self):
        with contextlib.redirect_stdout(io.StringIO()):
            run_init(self.root, test="unittest")
        assent_dir = self.root / ".assent"
        (assent_dir / "instructions.md").write_text(
            (_PROJECT_ROOT / "assent/templates/instructions.md").read_text(
                encoding="utf-8"), encoding="utf-8", newline="\n")
        (assent_dir / "format.md").write_text(
            "an older project's plan format\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)
        text = output.getvalue()
        self.assertFalse((assent_dir / "instructions.md").exists())
        self.assertIn(f"Removed: {assent_dir / 'instructions.md'}", text)
        self.assertEqual(
            (assent_dir / "format.md").read_text(encoding="utf-8"),
            "an older project's plan format\n")
        self.assertIn(f"Warning: {assent_dir / 'format.md'}", text)
        self.assertIn(str(self.user_home / "format.md"), text)
        # A rerun changes nothing further: the kept copy stays, the removed one
        # is not recreated.
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root), 0)
        self.assertFalse((assent_dir / "instructions.md").exists())
        self.assertEqual(
            (assent_dir / "format.md").read_text(encoding="utf-8"),
            "an older project's plan format\n")

    def test_an_older_bridge_line_is_replaced_in_place(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n- 保留這行\n"
            "- When using assent, first read `.assent/instructions.md` in the "
            "project's main worktree. <!-- assent-instructions -->\n"
            "- 也保留這行\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            run_init(self.root, test="unittest")
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("<!-- assent-instructions -->"), 1)
        self.assertIn("- 保留這行\n", text)
        self.assertIn("- 也保留這行", text)
        self.assertIn("`~/.assent/instructions.md`", text)
        self.assertNotIn("`.assent/instructions.md`", text)
        self.assertIn("An AI session never initiates the full suite", text)

    def test_adds_one_bridge_line_to_existing_agents_md(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n既有規則。\n", encoding="utf-8")
        run_init(self.root, test="unittest")
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 我的專案"))
        self.assertIn("既有規則。", text)
        self.assertEqual(text.count("<!-- assent-instructions -->"), 1)

    def test_new_agents_md_combines_template_with_managed_bridge(self):
        run_init(self.root, test="unittest")
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        template = (_PROJECT_ROOT / "assent/templates/AGENTS.md").read_text(
            encoding="utf-8")
        self.assertIn(template.rstrip(), text)
        self.assertEqual(text.count("<!-- assent-instructions -->"), 1)
        self.assertNotIn("<!-- assent-instructions -->", template)

    def test_preserves_existing_ignore_lines_and_agents_md_ignore_choice(self):
        (self.root / ".gitignore").write_text(
            "cache/\nAGENTS.md\n", encoding="utf-8")
        run_init(self.root, test="unittest")
        lines = (self.root / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn("cache/", lines)
        self.assertIn(".assent/", lines)
        self.assertIn("AGENTS.md", lines)

    def test_missing_target_dir_fails(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root / "nope"), 1)

    def test_no_git_refuses_without_creating_files(self):
        target = self.root / "not-repo"
        target.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(target, test="unittest"), 1)
        self.assertIn("This project has no git repository yet; run git init first",
                      out.getvalue())
        self.assertFalse((target / ".assent").exists())


if __name__ == "__main__":
    unittest.main()
