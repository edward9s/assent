import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path

from agents.terminal_log import (
    log_path_for_argv, sanitize_log_text, terminal_logging,
)


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
        config = self.root / ".agents" / "agents.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")
        return config

    def write_task(self, folder="plan01", status="TODO"):
        task = self.root / ".agents" / folder / "t001_task.e.toml"
        task.parent.mkdir(parents=True, exist_ok=True)
        task.write_text(
            'title = "任務"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["agents/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "完成"\n'
            'acceptance = "通過"\n', encoding="utf-8")
        return task

    def test_unique_ongoing_folder_selects_log_path(self):
        config = self.write_config()
        self.write_task()
        expected = self.root.resolve() / ".agents" / "plan01" / "_agents.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--config", str(config)]),
            expected)

    def test_config_option_and_folder_override_select_log_path(self):
        config = self.write_config()
        expected = self.root.resolve() / ".agents" / "parallel02" / "_agents.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--task", "t001", "parallel02", f"--config={config}"]),
            expected)

    def test_run_all_uses_parent_log_instead_of_a_folder_log(self):
        config = self.write_config()
        self.write_task("only")
        expected = self.root.resolve() / ".agents" / "_agents.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--all", "--jobs", "2", "--config", str(config)]),
            expected)

    def test_missing_or_bad_config_falls_back_beside_config(self):
        config = self.root / ".agents" / "agents.toml"
        expected = self.root.resolve() / ".agents" / "_agents.log"
        self.assertEqual(log_path_for_argv(["run", "--config", str(config)]),
                         expected)
        self.write_config("[plan\ntasks =")
        self.assertEqual(log_path_for_argv(["run", "--config", str(config)]),
                         expected)
        config.write_bytes(b"[plan]\ntasks = \xff\n")
        self.assertEqual(log_path_for_argv(["run", "--config", str(config)]),
                         expected)

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
        self.assertIn("AGENTS START", logged)
        self.assertIn("COMMAND      | agents run", logged)
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
                    "\r  額度重置:倒數 00:00:03 後重跑...")
                self.assertNotIn("倒數", path.read_text(encoding="utf-8"))
        self.assertIn("倒數 00:00:03", terminal.getvalue())

    def test_each_run_truncates_previous_log(self):
        config = self.write_config()
        self.write_task()
        with terminal_logging(["run", "--config", str(config)]) as path:
            print("第一次現場")
        with terminal_logging(["run", "--config", str(config)]):
            print("第二次現場")
        logged = path.read_text(encoding="utf-8")
        self.assertNotIn("第一次現場", logged)
        self.assertIn("第二次現場", logged)

    def test_non_run_commands_do_not_create_or_change_log(self):
        config = self.write_config()
        self.write_task()
        log_path = self.root / ".agents" / "plan01" / "_agents.log"
        for command in ("status", "check", "report", "init"):
            with terminal_logging([command, "--config", str(config)]):
                print(f"{command} 輸出")
            self.assertFalse(log_path.exists())

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("既有 run 現場", encoding="utf-8")
        for command in ("status", "check", "report"):
            with terminal_logging([command, "--config", str(config)]):
                print(f"{command} 輸出")
        self.assertEqual(log_path.read_text(encoding="utf-8"), "既有 run 現場")


if __name__ == "__main__":
    unittest.main()
