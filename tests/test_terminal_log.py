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

    def test_config_parent_selects_log_path(self):
        expected = self.root.resolve() / ".agents" / "agents.log"
        self.assertEqual(log_path_for_argv(
            ["run", "--config", str(self.root / ".agents" / "agents.toml")]),
            expected)

    def test_stdout_stderr_are_flushed_incrementally_with_start_header(self):
        terminal_out = io.StringIO()
        terminal_err = io.StringIO()
        config = self.root / "agents.toml"
        with contextlib.redirect_stdout(terminal_out), contextlib.redirect_stderr(terminal_err):
            with terminal_logging(["check", "--config", str(config)]) as path:
                print("\x1b[32mAI| hello " + chr(0x1F680) + "\x1b[0m", end="")
                print("error", file=__import__("sys").stderr)
                # The data is visible before the context exits: writes are incremental.
                during = path.read_text(encoding="utf-8")
                self.assertIn("AI| hello ", during)
                self.assertIn("error", during)
        logged = (self.root / "agents.log").read_text(encoding="utf-8")
        self.assertIn("AGENTS START", logged)
        self.assertIn("COMMAND      | agents check", logged)
        self.assertNotIn("\x1b", logged)
        self.assertNotIn(chr(0x1F680), logged)
        self.assertIn("AI| hello " + chr(0x1F680), terminal_out.getvalue())

    def test_terminal_only_output_is_not_logged(self):
        terminal = io.StringIO()
        config = self.root / "agents.toml"
        with contextlib.redirect_stdout(terminal):
            with terminal_logging(["run", "--config", str(config)]) as path:
                __import__("sys").stdout.write_terminal_only(
                    "\r  額度重置:倒數 00:00:03 後重跑...")
                self.assertNotIn("倒數", path.read_text(encoding="utf-8"))
        self.assertIn("倒數 00:00:03", terminal.getvalue())


if __name__ == "__main__":
    unittest.main()
