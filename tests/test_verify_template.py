"""Fixture tests for the packaged verify.py template: its git diff gates,
its run() command resolution, and its run_unittest_parallel() helper."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "assent/templates/verify.py"

_PASS_MODULE = (
    "import unittest\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        self.assertTrue(True)\n"
)

_FAIL_MODULE = (
    "import unittest\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_fail(self):\n"
    "        self.fail('deliberate fixture failure marker')\n"
)


class VerifyTemplateFixture(unittest.TestCase):
    """Builds a throwaway git repo running the packaged template as run_verify.py."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="assent verify template "))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)
        for key, value in (("user.name", "Verify Template Test"),
                           ("user.email", "verify-template@example.invalid"),
                           ("core.autocrlf", "false")):
            subprocess.run(["git", "config", key, value], cwd=self.root,
                           check=True, capture_output=True)
        # Repository-local core.autocrlf=false plus "* -text" keeps every blob
        # byte-identical to the worktree file, so the LF and CRLF fixtures below
        # mean the same thing on any operator's machine.
        (self.root / ".gitattributes").write_bytes(b"* -text\n")

        template_text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("# run_unittest_parallel()", template_text)
        script_text = template_text.replace(
            "# run_unittest_parallel()", "run_unittest_parallel()")
        self.script = self.root / "run_verify.py"
        self.script.write_text(script_text, encoding="utf-8")

        self.tests_dir = self.root / "tests"
        self.tests_dir.mkdir()

    def _write_module(self, name: str, source: str) -> None:
        (self.tests_dir / f"{name}.py").write_text(source, encoding="utf-8")

    def _write_bytes(self, name: str, data: bytes) -> None:
        (self.root / name).write_bytes(data)

    def _commit(self, message: str) -> None:
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=self.root,
                       check=True, capture_output=True)

    def _run(self, env_overrides: dict[str, str] | None = None
             ) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env.pop("ASSENT_VERIFY_JOBS", None)
        if env_overrides:
            env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(self.script)], cwd=self.root,
            capture_output=True, encoding="utf-8", errors="replace", env=env)


class RunUnittestParallelCase(VerifyTemplateFixture):
    def test_packaged_project_test_examples_are_all_commented(self) -> None:
        lines = {
            line.strip() for line in TEMPLATE.read_text(encoding="utf-8").splitlines()
        }
        examples = (
            "run_unittest_parallel()",
            'run("pytest")',
            'run("npm", "test")',
            'run("flutter", "test")',
        )
        for example in examples:
            with self.subTest(example=example):
                self.assertIn(f"# {example}", lines)
                self.assertNotIn(example, lines)

    def test_no_unittest_modules_fails_instead_of_reporting_success(self) -> None:
        self._commit("empty test fixture")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("no test_*.py modules found", result.stdout)
        self.assertNotIn("verify: OK", result.stdout)

    def test_all_green_exits_zero_with_sorted_summary(self) -> None:
        self._write_module("test_b", _PASS_MODULE)
        self._write_module("test_a", _PASS_MODULE)
        self._commit("all green fixture")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line_a = f"test_a: pass ("
        line_b = f"test_b: pass ("
        pos_a = result.stdout.index(line_a)
        pos_b = result.stdout.index(line_b)
        self.assertLess(pos_a, pos_b)
        self.assertIn("verify: OK", result.stdout)

    def test_one_red_one_green_runs_both_and_shows_failure_output(self) -> None:
        self._write_module("test_a", _PASS_MODULE)
        self._write_module("test_b", _FAIL_MODULE)
        self._commit("mixed fixture")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        combined = result.stdout + result.stderr
        self.assertIn("test_a: pass (", result.stdout)
        self.assertIn("test_b: fail (", result.stdout)
        self.assertIn("deliberate fixture failure marker", combined)
        self.assertIn("verify: FAIL", result.stdout)
        self.assertNotIn("verify: OK", result.stdout)

    def test_assent_verify_jobs_one_is_honored(self) -> None:
        self._write_module("test_a", _PASS_MODULE)
        self._write_module("test_b", _PASS_MODULE)
        self._commit("jobs one fixture")

        result = self._run(env_overrides={"ASSENT_VERIFY_JOBS": "1"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("test_a: pass (", result.stdout)
        self.assertIn("test_b: pass (", result.stdout)

    def test_invalid_assent_verify_jobs_falls_back_without_error(self) -> None:
        self._write_module("test_a", _PASS_MODULE)
        self._write_module("test_b", _PASS_MODULE)
        self._commit("invalid jobs fixture")

        result = self._run(env_overrides={"ASSENT_VERIFY_JOBS": "not-a-number"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("test_a: pass (", result.stdout)
        self.assertIn("test_b: pass (", result.stdout)


class ResolvedCommandCase(VerifyTemplateFixture):
    """run() resolves its program through PATH/PATHEXT before spawning it."""

    def _write_script_with_command(self, call: str) -> None:
        """Rewrite the fixture script with one extra run() call before the OK line."""
        template_text = TEMPLATE.read_text(encoding="utf-8")
        marker = 'print("verify: OK")'
        self.assertIn(marker, template_text)
        self.script.write_text(
            template_text.replace(marker, f"{call}\n{marker}"), encoding="utf-8")

    def test_missing_command_fails_closed_without_traceback(self) -> None:
        self._write_script_with_command(
            'run("assent-no-such-command-fixture", "--version")')
        self._commit("missing command fixture")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("verify: FAIL", result.stdout)
        self.assertIn("assent-no-such-command-fixture", result.stdout)
        self.assertNotIn("verify: OK", result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)

    @unittest.skipUnless(sys.platform == "win32",
                         "PATHEXT .bat resolution is Windows-specific")
    def test_path_provided_bat_command_runs(self) -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "assentfixture.bat").write_text(
            "@echo off\r\necho fixture bat ran %1\r\n", encoding="ascii")
        self._write_script_with_command('run("assentfixture", "alpha")')
        self._commit("bat command fixture")

        path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        result = self._run(env_overrides={"PATH": path})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture bat ran alpha", result.stdout)
        self.assertIn("verify: OK", result.stdout)


class DiffIntegrityCase(VerifyTemplateFixture):
    """Whitespace passes, while both diff gates reject conflict markers."""

    def _prepare(self, baseline: bytes) -> None:
        self._write_module("test_a", _PASS_MODULE)
        self._write_bytes("data.txt", baseline)
        self._commit("line ending baseline")

    def test_clean_lf_worktree_delta_passes(self) -> None:
        self._prepare(b"alpha\n")
        self._write_bytes("data.txt", b"alpha\nbeta\n")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify: OK", result.stdout)

    def test_clean_crlf_worktree_delta_passes(self) -> None:
        self._prepare(b"alpha\r\n")
        self._write_bytes("data.txt", b"alpha\r\nbeta\r\n")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify: OK", result.stdout)

    def test_clean_crlf_committed_delta_passes(self) -> None:
        self._prepare(b"alpha\r\n")
        self._write_bytes("data.txt", b"alpha\r\nbeta\r\n")
        self._commit("crlf candidate")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify: OK", result.stdout)

    def test_trailing_space_before_lf_passes(self) -> None:
        self._prepare(b"alpha\n")
        self._write_bytes("data.txt", b"alpha\nbeta \n")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify: OK", result.stdout)

    def test_trailing_tab_before_crlf_in_committed_delta_passes(self) -> None:
        self._prepare(b"alpha\r\n")
        self._write_bytes("data.txt", b"alpha\r\nbeta\t\r\n")
        self._commit("crlf whitespace candidate")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify: OK", result.stdout)

    def test_blank_line_at_eof_in_committed_delta_passes(self) -> None:
        self._prepare(b"alpha\n")
        self._write_bytes("data.txt", b"alpha\nbeta\n\n")
        self._commit("blank eof candidate")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("verify: OK", result.stdout)

    def test_worktree_conflict_marker_still_fails(self) -> None:
        self._prepare(b"alpha\n")
        self._write_bytes(
            "data.txt",
            b"alpha\n<<<<<<< ours\nbeta\n=======\ngamma\n>>>>>>> theirs\n",
        )

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("leftover conflict marker", result.stdout + result.stderr)
        self.assertIn("verify: FAIL", result.stdout)
        self.assertNotIn("verify: OK", result.stdout)

    def test_committed_conflict_marker_still_fails(self) -> None:
        self._prepare(b"alpha\n")
        self._write_bytes(
            "data.txt",
            b"alpha\n<<<<<<< ours\nbeta\n=======\ngamma\n>>>>>>> theirs\n",
        )
        self._commit("conflict marker candidate")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        self.assertIn("leftover conflict marker", result.stdout + result.stderr)
        self.assertIn("verify: FAIL", result.stdout)
        self.assertNotIn("verify: OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
