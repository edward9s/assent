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
from assent.doctor import doctor


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
        self.assertIn("git: FAIL", out)

    @patch("assent.doctor.get_adapter")
    def test_temp_dir_not_writable_is_hard_failure(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": True, "codex": True, "antigravity": True})
        missing = Path(tempfile.gettempdir()) / "assent-doctor-missing-dir-xyz"
        with patch("assent.doctor.tempfile.gettempdir",
                   return_value=str(missing)):
            code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("temp directory: FAIL", out)

    @patch("assent.doctor.get_adapter")
    def test_all_adapters_failing_is_hard_failure(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": False, "codex": False, "antigravity": False})
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("claude: FAIL", out)
        self.assertIn("codex: FAIL", out)
        self.assertIn("antigravity: FAIL", out)

    @patch("assent.doctor.get_adapter")
    def test_one_adapter_succeeding_is_enough(self, mock_get_adapter):
        mock_get_adapter.side_effect = _fake_get_adapter(
            {"claude": False, "codex": True, "antigravity": False})
        code, out = self._run()
        self.assertEqual(code, 0)
        self.assertIn("codex: OK", out)
        self.assertIn("claude: FAIL", out)
        self.assertIn("antigravity: FAIL", out)


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
