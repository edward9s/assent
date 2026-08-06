"""Tests for assent doctor: machine-environment diagnosis without a project.

Adapter CLI probing is faked here (never assume claude/codex/agy are actually
installed on the test machine); git and the temp directory are exercised
through the real subprocess/tempfile calls, patched only for the failure
scenarios."""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent.__main__ import main
from assent.doctor import FAIL, PASS, WARN, _print_check, doctor


class FakeAdapter:
    def __init__(self, ok: bool, message: str = "probe result"):
        self._ok = ok
        self._message = message

    def probe_cli(self):
        return self._ok, self._message


def _fake_get_adapter(results):
    def _get(name, cfg):
        return FakeAdapter(results[name])
    return _get


class DoctorExitCodeTests(unittest.TestCase):
    def _run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor()
        return code, out.getvalue()

    @patch("assent.doctor.get_adapter")
    def test_git_not_found_is_hard_failure(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": True, "codex": True, "antigravity": True})
        with patch("assent.doctor.subprocess.run",
                   side_effect=FileNotFoundError()):
            code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("[X] git:", out)

    @patch("assent.doctor.get_adapter")
    def test_temp_dir_not_writable_is_hard_failure(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": True, "codex": True, "antigravity": True})
        missing = Path(tempfile.gettempdir()) / "assent-doctor-missing-dir-xyz"
        with patch("assent.doctor.tempfile.gettempdir",
                   return_value=str(missing)):
            code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("[X] temp directory:", out)

    @patch("assent.doctor.get_adapter")
    def test_all_adapters_failing_is_hard_failure(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": False, "codex": False, "antigravity": False})
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("[X] claude:", out)
        self.assertIn("[X] codex:", out)
        self.assertIn("[X] antigravity:", out)

    @patch("assent.doctor.get_adapter")
    def test_one_adapter_succeeding_is_enough(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": False, "codex": True, "antigravity": False})
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("[OK] codex:", out)
        self.assertIn("[X] claude:", out)
        self.assertIn("[X] antigravity:", out)


class StubStream(io.StringIO):
    """A capture stream that declares a chosen encoding and TTY-ness, so glyph
    and colour degradation are provable without a real terminal."""

    def __init__(self, encoding: str | None, tty: bool):
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self):
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


def _render(state, encoding, tty):
    stream = StubStream(encoding, tty)
    _print_check(state, "check", "detail", stream=stream)
    return stream.getvalue()


class DoctorMarkerTests(unittest.TestCase):
    def test_utf8_tty_gets_preferred_glyphs_and_colour(self):
        self.assertEqual(_render(PASS, "utf-8", True),
                         "\x1b[32m[√]\x1b[0m check: detail\n")
        self.assertEqual(_render(WARN, "utf-8", True),
                         "\x1b[33m[!]\x1b[0m check: detail\n")
        self.assertEqual(_render(FAIL, "utf-8", True),
                         "\x1b[31m[×]\x1b[0m check: detail\n")

    def test_restricted_encoding_falls_back_to_exact_ascii_markers(self):
        for state, marker in ((PASS, "[OK]"), (WARN, "[!]"), (FAIL, "[X]")):
            with self.subTest(state=state):
                out = _render(state, "ascii", False)
                self.assertEqual(out, f"{marker} check: detail\n")
                self.assertTrue(out.isascii())

    def test_non_tty_emits_no_escape_sequence(self):
        for encoding in ("utf-8", "ascii", None):
            for state in (PASS, WARN, FAIL):
                with self.subTest(encoding=encoding, state=state):
                    self.assertNotIn("\x1b", _render(state, encoding, False))

    def test_capable_non_tty_keeps_glyphs_without_colour(self):
        self.assertEqual(_render(PASS, "utf-8", False), "[√] check: detail\n")
        self.assertEqual(_render(FAIL, "utf-8", False), "[×] check: detail\n")

    def test_restricted_tty_keeps_colour_with_ascii_markers(self):
        self.assertEqual(_render(PASS, "cp437", True),
                         "\x1b[32m[OK]\x1b[0m check: detail\n")
        self.assertEqual(_render(FAIL, "cp437", True),
                         "\x1b[31m[X]\x1b[0m check: detail\n")

    def test_stream_without_encoding_falls_back_to_ascii(self):
        self.assertEqual(_render(PASS, None, False), "[OK] check: detail\n")


class DoctorCommandTests(unittest.TestCase):
    def test_runs_without_a_project_config(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        old_cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, old_cwd)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["doctor"])
        output = out.getvalue()

        self.assertNotIn("Config error", output)
        self.assertIn("Python:", output)
        self.assertIn("git:", output)
        self.assertIn("claude:", output)
        self.assertIn("codex:", output)
        self.assertIn("antigravity:", output)
        self.assertIn("temp directory:", output)
