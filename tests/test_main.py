"""CLI entry point and init tests. ``run`` tees to the work folder's
_assent.log, so tests always chdir into a temporary directory to avoid
dirtying the test process's own working directory.

Chinese literals that remain are deliberate user-authored data (task titles,
goals, acceptance text, rework reasons) used to prove that non-English data
passes through the CLI verbatim rather than being translated as output."""
import contextlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assent.__main__ import _start_stdin_stop_watcher, main
from assent.init import init as run_init

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_HAN_CHAR_RE = re.compile(r"[一-鿿]")


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._old_cwd)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

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
    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_output_is_english_for_top_level_and_every_subcommand(self):
        commands = ("run", "status", "check", "report", "verify", "clean", "accept",
                    "reject", "rework", "init")
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

    def test_run_all_and_folder_are_mutually_exclusive(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(
                io.StringIO()):
            main(["run", "work", "--all"])
        self.assertEqual(ctx.exception.code, 2)

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

    def test_all_plan_commands_accept_folder_override(self):
        config = self.write_config()
        assent_dir = config.parent
        commands = (("run", "run"), ("status", "status"),
                    ("check", "check"), ("report", "report"))
        for command, engine_name in commands:
            with self.subTest(command=command), patch(
                    f"assent.__main__.engine.{engine_name}", return_value=0) as mocked:
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

    def test_accept_requires_exactly_one_folder_and_has_no_remote_options(self):
        for argv in (["accept"], ["accept", "one", "two"],
                     ["accept", "one", "--all"],
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

    def test_verify_requires_one_folder_and_has_no_all_option(self):
        for argv in (["verify"], ["verify", "one", "two"],
                     ["verify", "one", "--all"]):
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
                    f"assent.__main__.engine.{command}", return_value=0) as mocked:
                code, _ = self.run_main([command, "--config", str(config)])
                self.assertEqual(code, 0)
                self.assertEqual(
                    [call.args[0].tasks_name for call in mocked.call_args_list],
                    ["alpha", "beta"])

    def test_check_without_folder_fails_if_any_folder_fails(self):
        config = self.write_config()
        self.write_task("alpha")
        self.write_task("beta")
        with patch("assent.__main__.engine.check", side_effect=[0, 1]) as mocked:
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
                with patch("assent.__main__.engine.check", return_value=0):
                    code, out = self.run_main(
                        ["check", "--config", str(config)])
                self.assertEqual(code, 1)
                self.assertIn("Folder dependency graph: FAIL", out)


class TestStdinStopWatcher(unittest.TestCase):
    """The stdin stop channel is opt-in: only a scheduler-spawned child gets
    ASSENT_STDIN_STOP, so a hand-typed `assent run` keeps its stdin."""

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


class TestInit(MainTestCase):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)

    def test_creates_skeleton(self):
        code, out = self.run_main(["init"])
        self.assertEqual(code, 0)
        for rel in (".assent/assent.toml", ".assent/format.md",
                    ".assent/instructions.md", ".assent/verify.py",
                    "AGENTS.md", ".gitignore"):
            self.assertTrue((self.root / rel).is_file(), rel)
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
        config = (self.root / ".assent" / "assent.toml").read_text(
            encoding="utf-8")
        self.assertNotIn("[git]", config)
        self.assertNotIn("[plan]", config)

    def test_idempotent_no_overwrite_no_duplicates(self):
        run_init(self.root)
        (self.root / ".assent" / "assent.toml").write_text(
            '[run]\nretry_per_task = 7\n# custom\n', encoding="utf-8")
        (self.root / ".assent" / "instructions.md").write_text(
            "本機自訂指示\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root), 0)
        # Does not overwrite the existing config.
        self.assertIn("custom", (self.root / ".assent" / "assent.toml")
                      .read_text(encoding="utf-8"))
        # gitignore entries are not duplicated on re-run.
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gitignore.splitlines().count(".assent/"), 1)
        # An existing file is not overwritten, and the AGENTS.md bridge is not duplicated.
        self.assertEqual((self.root / ".assent" / "instructions.md")
                         .read_text(encoding="utf-8"), "本機自訂指示\n")
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md.count("<!-- assent-instructions -->"), 1)

    def test_init_does_not_overwrite_existing_verifier(self):
        run_init(self.root)
        verifier = self.root / ".assent" / "verify.py"
        custom = "# project-specific verifier\n"
        verifier.write_text(custom, encoding="utf-8")
        self.assertEqual(run_init(self.root), 0)
        self.assertEqual(verifier.read_text(encoding="utf-8"), custom)

    def test_adds_one_bridge_line_to_existing_agents_md(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n既有規則。\n", encoding="utf-8")
        run_init(self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 我的專案"))
        self.assertIn("既有規則。", text)
        self.assertEqual(text.count("<!-- assent-instructions -->"), 1)

    def test_preserves_existing_ignore_lines_and_agents_md_ignore_choice(self):
        (self.root / ".gitignore").write_text(
            "cache/\nAGENTS.md\n", encoding="utf-8")
        run_init(self.root)
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
            self.assertEqual(run_init(target), 1)
        self.assertIn("This project has no git repository yet; run git init first",
                      out.getvalue())
        self.assertFalse((target / ".assent").exists())


if __name__ == "__main__":
    unittest.main()
