"""CLI entry point and init tests. ``run`` tees to the work folder's
_assent.log, so tests always chdir into a temporary directory to avoid
dirtying the test process's own working directory.

Chinese literals that remain are deliberate user-authored data (task titles,
goals, acceptance text, rework reasons) used to prove that non-English data
passes through the CLI verbatim rather than being translated as output."""
import contextlib
import io
import importlib.metadata
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
from assent.init import init as run_init
from tests.test_contracts import install_global_contracts

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

    def test_missing_config_reports_error(self):
        code, out = self.run_main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("Config error", out)
        self.assertIn("assent init", out)

    def test_run_missing_config_reports_error(self):
        code, out = self.run_main(
            ["run", "--config", str(self.root / "nope" / "assent.toml")])
        self.assertEqual(code, 1)

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
        with patch("assent.__main__.engine.run", return_value=0), patch(
                "assent.__main__.run_all", return_value=0) as mocked:
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
        with patch("assent.__main__.run_all", return_value=0) as mocked:
            code, _ = self.run_main(["run", "--all", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[2], 1)

    def test_run_named_folders_dispatch_in_given_order(self):
        config = self.write_config()
        with patch("assent.__main__.engine.run", return_value=0) as mocked:
            code, _ = self.run_main(
                ["run", "first", "second", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(
            [call.args[0].tasks_name for call in mocked.call_args_list],
            ["first", "second"])

    def test_run_named_folders_stops_after_first_failure(self):
        config = self.write_config()
        with patch("assent.__main__.engine.run", side_effect=[1, 0]) as mocked:
            code, _ = self.run_main(
                ["run", "first", "second", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual(
            [call.args[0].tasks_name for call in mocked.call_args_list],
            ["first"])

    def test_run_named_folders_with_all_runs_remainder_once(self):
        config = self.write_config()
        with patch("assent.__main__.engine.run", return_value=0) as run_mock, \
                patch("assent.__main__.run_all", return_value=0) as all_mock:
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
        with patch("assent.__main__.accept_folder", return_value=0) as mocked:
            code, _ = self.run_main(
                ["accept", "reviewed", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "reviewed")

    def test_accept_dispatches_two_or_more_folders_as_selected_batch(self):
        config = self.write_config()
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

    def test_verify_dispatches_explicit_folder_and_preserves_exit_code(self):
        config = self.write_config()
        with patch("assent.__main__.verify_folder", side_effect=[0, 1]) as mocked:
            codes = [self.run_main(
                ["verify", "reviewed", "--config", str(config)])[0]
                     for _ in range(2)]
        self.assertEqual(codes, [0, 1])
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "reviewed")

    def test_verify_dispatches_exact_selected_batch_and_focus(self):
        config = self.write_config()
        with patch("assent.__main__.verify_selected_batch", return_value=0) as batch:
            code, _ = self.run_main([
                "verify", "later", "earlier", "--no-bisect",
                "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(batch.call_args.args[:2],
                         (str(config), config.parent.resolve()))
        self.assertEqual(batch.call_args.args[2], ["later", "earlier"])
        self.assertFalse(batch.call_args.args[3])

        with patch("assent.__main__.engine.verify_focused", return_value=0) as focus:
            code, _ = self.run_main([
                "verify", "reviewed", "--focus", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(focus.call_args.args[0].tasks_name, "reviewed")

    def test_verify_batch_dispatches_bisect_and_keeps_the_default_prompt(self):
        config = self.write_config()
        with patch("assent.__main__.verify_batch", return_value=0) as mocked:
            code, _ = self.run_main(["verify", "--batch", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[2], True)
        # No confirmation callback is injected, so the CLI keeps the terminal
        # `input` default that asks about skipping a conflicting source.
        self.assertEqual(mocked.call_args.kwargs, {})

    def test_verify_batch_help_states_the_conflict_skip_confirmation(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(
                output):
            main(["verify", "-h"])
        self.assertEqual(ctx.exception.code, 0)
        text = " ".join(output.getvalue().split())
        self.assertIn("a conflicting source is reported and, after one "
                      "confirmation, skipped together with the folders queued "
                      "after it", text)
        self.assertNotIn("accept the folders ahead", text)

    def test_verify_interrupt_returns_130(self):
        config = self.write_config()
        with patch("assent.__main__.verify_folder", side_effect=KeyboardInterrupt):
            code, out = self.run_main(
                ["verify", "reviewed", "--config", str(config)])
        self.assertEqual(code, 130)
        self.assertIn("temporary resources were cleaned up", out)

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
        with patch("assent.__main__.reject_folder", return_value=0) as mocked:
            code, _ = self.run_main(["reject", "B", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "B")

    def test_reconcile_routes_each_form_to_its_own_lifecycle(self):
        config = self.write_config()
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
        self.assertIn("FOLDER", text)
        for option in ("--continue", "--abort", "--config"):
            self.assertIn(option, text)
        # argparse wraps the description, so compare without its line breaks.
        unwrapped = " ".join(text.split())
        self.assertIn("never runs the focused or the complete verification",
                      unwrapped)
        self.assertIn("assent verify FOLDER", unwrapped)
        self.assertIn("assent accept FOLDER", unwrapped)

    def test_reconcile_configuration_error_returns_one_without_dispatch(self):
        config = self.write_config()
        with patch("assent.__main__.reconcile_start") as mocked:
            code, out = self.run_main(
                ["reconcile", "bad/name", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("Config error", out)
        mocked.assert_not_called()

    def test_rework_help_shows_only_formal_syntax_and_options(self):
        output = io.StringIO()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(output):
            main(["rework", "-h"])
        self.assertEqual(ctx.exception.code, 0)
        text = output.getvalue()
        self.assertIn("assent rework", text)
        self.assertIn("FOLDER TASK", text)
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
        self.assertIn("Config error", out)
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
        config = (self.user_home / "assent.toml").read_text(encoding="utf-8")
        self.assertNotIn("[git]", config)
        self.assertNotIn("[plan]", config)
        verifier = (self.root / ".assent" / "verify.py").read_text(
            encoding="utf-8")
        self.assertIn("\nrun_unittest_parallel()\n", verifier)
        self.assertIn("\n# run(\"pytest\")\n", verifier)
        self.assertIn("\n# run(\"npm\", \"test\")\n", verifier)
        self.assertIn("\n# run(\"flutter\", \"test\")\n", verifier)
        self.assertIn("Created:", out)

    def test_builtin_test_choices_activate_only_the_selected_command(self):
        choices = {
            "unittest": "run_unittest_parallel()",
            "pytest": 'run("pytest")',
            "npm": 'run("npm", "test")',
            "flutter": 'run("flutter", "test")',
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
        self.assertIn("1. Parallel unittest", output)
        self.assertIn("5. Custom command", output)
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
            '[run]\nretry_per_task = 7\ncustom_setting = "keep"\n',
            encoding="utf-8")
        (self.user_home / "instructions.md").write_text(
            "an older assent's working instructions\n", encoding="utf-8")
        (self.user_home / "format.md").write_text(
            "old format\n", encoding="utf-8")
        verifier_before = (self.root / ".assent" / "verify.py").read_bytes()
        out = io.StringIO()
        with patch("builtins.input", side_effect=AssertionError("prompted")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root), 0)
        config_text = user_config.read_text(encoding="utf-8")
        config = tomllib.loads(config_text)
        self.assertEqual(config["run"]["retry_per_task"], 7)
        self.assertEqual(config["run"]["custom_setting"], "keep")
        self.assertIn("quota_poll_minutes", config["run"])
        self.assertIn("watchdog", config)
        config_after_first_upgrade = user_config.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
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
        with patch("builtins.input", side_effect=AssertionError("prompted")):
            self.assertEqual(run_init(self.root), 0)
        self.assertEqual(verifier.read_text(encoding="utf-8"), custom)

    def test_test_option_refuses_when_verifier_already_exists(self):
        run_init(self.root, test="unittest")
        verifier = self.root / ".assent" / "verify.py"
        before = {path: path.read_bytes() for path in (
            verifier, self.user_home / "format.md",
            self.user_home / "instructions.md",
            self.user_home / "assent.toml")}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root, test="pytest"), 1)
        self.assertIn("refusing --test", output.getvalue())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

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
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 1)
        self.assertIn("not valid TOML", output.getvalue())
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)

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
        with contextlib.redirect_stdout(io.StringIO()):
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
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)
        self.assertEqual(project_config.read_bytes(), before)
        text = output.getvalue()
        self.assertIn(f"Warning: {project_config}", text)
        self.assertIn(str(self.user_home / "assent.toml"), text)

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

    def test_adds_one_bridge_line_to_existing_agents_md(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n既有規則。\n", encoding="utf-8")
        run_init(self.root, test="unittest")
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 我的專案"))
        self.assertIn("既有規則。", text)
        self.assertEqual(text.count("<!-- assent-instructions -->"), 1)

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
