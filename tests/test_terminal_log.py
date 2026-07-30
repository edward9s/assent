import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent.terminal_log import (
    log_path_for_argv, sanitize_log_text, terminal_logging,
)
from assent.user_home import ASSENT_HOME_ENV


class TestSanitize(unittest.TestCase):
    def test_removes_ansi_controls_and_emoji(self):
        raw = "\x1b[31mred\x1b[0m\tOK\rnext\x00 " + chr(0x1F680) + "\n"
        clean = sanitize_log_text(raw)
        self.assertEqual(clean, "red    OK\nnext \n")
        self.assertNotIn("\x1b", clean)


class TestTerminalLogging(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write_config(self, text=""):
        config = self.root / ".assent" / "assent.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")
        return config

    def write_task(self, folder="plan01", status="TODO"):
        task = self.root / ".assent" / folder / "t001_task.e.toml"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(
            'title = "task"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "done"\n'
            'acceptance = "passed"\n', encoding="utf-8")
        return task

    def test_unique_ongoing_folder_selects_log_path(self):
        config = self.write_config()
        self.write_task()
        expected = self.root.resolve() / ".assent" / "plan01" / "_assent.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--config", str(config)]),
            expected)

    def test_config_option_and_folder_override_select_log_path(self):
        config = self.write_config()
        self.write_task("parallel02")
        expected = self.root.resolve() / ".assent" / "parallel02" / "_assent.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--task", "t001", "parallel02", f"--config={config}"]),
            expected)

    def test_run_all_uses_parent_log_instead_of_a_folder_log(self):
        config = self.write_config()
        self.write_task("only")
        expected = self.root.resolve() / ".assent" / "_assent.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--all", "--jobs", "2", "--config", str(config)]),
            expected)

    def test_unresolved_explicit_folder_uses_management_log(self):
        config = self.write_config()
        expected = self.root.resolve() / ".assent" / "_assent.log"
        for command in ("run", "verify"):
            with self.subTest(command=command):
                argv = [command, "AA01", "--config", str(config)]
                self.assertEqual(log_path_for_argv(argv), expected)
                with terminal_logging(argv) as path:
                    print("selection refused")
                self.assertEqual(path, expected)
                self.assertFalse(
                    (self.root / ".assent" / "AA01").exists())

    def test_missing_or_bad_config_falls_back_beside_config(self):
        config = self.root / ".assent" / "assent.toml"
        expected = self.root.resolve() / ".assent" / "_assent.log"
        self.assertEqual(log_path_for_argv(["run", "--config", str(config)]),
                         expected)
        self.write_config("[plan\ntasks =")
        self.assertEqual(log_path_for_argv(["run", "--config", str(config)]),
                         expected)
        config.write_bytes(b"[plan]\ntasks = \xff\n")
        self.assertEqual(log_path_for_argv(["run", "--config", str(config)]),
                         expected)

    def test_log_stays_in_the_project_when_only_the_user_config_exists(self):
        # Settings may come entirely from ~/.assent, but a run's log is project
        # evidence: it belongs beside the task folder it was produced for.
        user_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, user_home, ignore_errors=True)
        (user_home / "assent.toml").write_text(
            '[adapter]\nname = "claude"\n', encoding="utf-8")
        environment = mock.patch.dict(
            os.environ, {ASSENT_HOME_ENV: str(user_home)})
        environment.start()
        self.addCleanup(environment.stop)
        self.write_task()  # a project task folder, but no project assent.toml
        old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old_cwd)

        path = log_path_for_argv(["run"])
        self.assertEqual(
            path, self.root.resolve() / ".assent" / "plan01" / "_assent.log")
        self.assertNotEqual(path.parent.parent, user_home)

    def test_explicit_config_still_selects_another_projects_management_dir(self):
        other = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        config = other / ".assent" / "assent.toml"
        config.parent.mkdir(parents=True)
        config.write_text("", encoding="utf-8")
        (other / ".assent" / "plan09").mkdir()
        (other / ".assent" / "plan09" / "t001_task.e.toml").write_text(
            (self.write_task()).read_text(encoding="utf-8"), encoding="utf-8")
        old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old_cwd)

        self.assertEqual(
            log_path_for_argv(["run", "--config", str(config)]),
            other.resolve() / ".assent" / "plan09" / "_assent.log")

    def test_stdout_stderr_are_flushed_incrementally_with_start_header(self):
        terminal_out = io.StringIO()
        terminal_err = io.StringIO()
        config = self.write_config()
        self.write_task()
        with contextlib.redirect_stdout(terminal_out), contextlib.redirect_stderr(terminal_err):
            with terminal_logging(["run", "--config", str(config)]) as path:
                print("\x1b[32mAI| hello " + chr(0x1F680) + "\x1b[0m", end="")
                print("error", file=__import__("sys").stderr)
                # The data is visible before the context exits: writes are incremental.
                during = path.read_text(encoding="utf-8")
                self.assertIn("AI| hello ", during)
                self.assertIn("error", during)
        logged = path.read_text(encoding="utf-8")
        self.assertIn("ASSENT START", logged)
        self.assertIn("COMMAND      | assent run", logged)
        self.assertNotIn("\x1b", logged)
        self.assertNotIn(chr(0x1F680), logged)
        self.assertIn("AI| hello " + chr(0x1F680), terminal_out.getvalue())

    def test_terminal_only_output_is_not_logged(self):
        terminal = io.StringIO()
        config = self.write_config()
        self.write_task()
        with contextlib.redirect_stdout(terminal):
            with terminal_logging(["run", "--config", str(config)]) as path:
                __import__("sys").stdout.write_terminal_only(
                    "\r  quota reset: retrying in 00:00:03...")
                self.assertNotIn("retrying", path.read_text(encoding="utf-8"))
        self.assertIn("retrying in 00:00:03", terminal.getvalue())

    def test_each_run_appends_a_separately_headed_section(self):
        config = self.write_config()
        self.write_task()
        with terminal_logging(["run", "--config", str(config)]) as path:
            print("first session")
        with terminal_logging(["run", "--config", str(config)]):
            print("second session")
        logged = path.read_text(encoding="utf-8")
        self.assertIn("first session", logged)
        self.assertIn("second session", logged)
        self.assertEqual(logged.count("ASSENT START"), 2)
        self.assertLess(logged.index("first session"),
                        logged.index("second session"))

    def test_non_run_commands_do_not_create_or_change_log(self):
        config = self.write_config()
        self.write_task()
        log_path = self.root / ".assent" / "plan01" / "_assent.log"
        for command in ("status", "check", "report", "init"):
            with terminal_logging([command, "--config", str(config)]):
                print(f"{command} output")
            self.assertFalse(log_path.exists())

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("existing run session", encoding="utf-8")
        for command in ("status", "check", "report"):
            with terminal_logging([command, "--config", str(config)]):
                print(f"{command} output")
        self.assertEqual(log_path.read_text(encoding="utf-8"), "existing run session")


if __name__ == "__main__":
    unittest.main()
