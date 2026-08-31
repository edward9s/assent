"""Fixture tests for the packaged verify.py template: its git diff gates,
its run() command resolution, and its run_unittest_parallel() helper."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).parents[1] / "assent/templates/verify.py"
_UNCONFIGURED_PROJECT_BLOCK = (
    "# assent-project-verifier: unconfigured\n"
    'fail("project verification is not configured")\n'
)

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

_UNICODE_FAIL_MODULE = (
    "import sys\n"
    "import unittest\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_fail(self):\n"
    "        print('繁體中文標準輸出')\n"
    "        print('繁體中文錯誤輸出', file=sys.stderr)\n"
    "        self.fail('繁體中文失敗診斷')\n"
)


def _sleep_module(seconds: float) -> str:
    return (
        "import time\n"
        "import unittest\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_ok(self):\n"
        f"        time.sleep({seconds})\n"
    )


_INVALID_BYTES_MODULE = (
    "import os\n"
    "os.write(1, b'native unittest stdout ' + bytes([0x80]) + b'\\n')\n"
    "os.write(2, b'native unittest stderr ' + bytes([0xff]) + b'\\n')\n"
    "import unittest\n\n"
    "class T(unittest.TestCase):\n"
    "    def test_ok(self):\n"
    "        pass\n"
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
        self.assertEqual(template_text.count(_UNCONFIGURED_PROJECT_BLOCK), 1)
        script_text = template_text.replace(
            _UNCONFIGURED_PROJECT_BLOCK, "run_unittest_parallel()\n")
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
            capture_output=True, encoding="utf-8", errors="strict", env=env)

    _RANK_LINE = re.compile(r"^ {2}(\d+)\. (\S+) (\d+\.\d\d)s (PASS|FAIL)$")
    RANKING_HEADER = "Slowest test modules:"

    def _ranking(self, stdout: str) -> list[tuple[int, str, float, str]]:
        """Parse the ranking block into (rank, module, seconds, status) rows."""
        self.assertIn(self.RANKING_HEADER, stdout)
        tail = stdout[stdout.index(self.RANKING_HEADER)
                      + len(self.RANKING_HEADER):]
        entries = []
        for line in tail.splitlines():
            match = self._RANK_LINE.match(line)
            if match is None:
                if line.strip():
                    break
                continue
            entries.append((int(match.group(1)), match.group(2),
                            float(match.group(3)), match.group(4)))
        return entries


class RunUnittestParallelCase(VerifyTemplateFixture):
    def test_packaged_project_test_examples_are_all_commented(self) -> None:
        template = TEMPLATE.read_text(encoding="utf-8")
        lines = {line.strip() for line in template.splitlines()}
        self.assertEqual(
            template.count("# --- Project test commands begin (project-owned) ---"),
            1)
        self.assertEqual(
            template.count("# --- Project test commands end ---"), 1)
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

    def test_all_green_exits_zero_and_reports_every_module(self) -> None:
        self._write_module("test_b", _PASS_MODULE)
        self._write_module("test_a", _PASS_MODULE)
        self._commit("all green fixture")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for line in ("test_a: pass (", "test_b: pass ("):
            self.assertIn(line, result.stdout)
            self.assertLess(result.stdout.index(line),
                            result.stdout.index(self.RANKING_HEADER))
        ranking = self._ranking(result.stdout)
        self.assertEqual([entry[1] for entry in ranking].count("test_a"), 1)
        self.assertEqual([entry[1] for entry in ranking].count("test_b"), 1)
        self.assertIn("verify: OK", result.stdout)

    def test_live_lines_follow_completion_order_before_the_ranking(self) -> None:
        # test_a_slow sorts first by name but finishes last, so a live line
        # ordered by completion is the only way test_z_fast can come first.
        self._write_module("test_a_slow", _sleep_module(1.5))
        self._write_module("test_z_fast", _PASS_MODULE)
        self._commit("live timing fixture")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        slow_live = result.stdout.index("test_a_slow: pass (")
        fast_live = result.stdout.index("test_z_fast: pass (")
        self.assertLess(fast_live, slow_live)
        self.assertLess(slow_live, result.stdout.index(self.RANKING_HEADER))

        ranking = self._ranking(result.stdout)
        self.assertEqual([entry[1] for entry in ranking],
                         ["test_a_slow", "test_z_fast"])
        self.assertEqual([entry[0] for entry in ranking], [1, 2])
        self.assertGreater(ranking[0][2], ranking[1][2])
        self.assertGreaterEqual(ranking[0][2], 1.5)

    def test_ranking_is_descending_and_breaks_ties_by_module_name(self) -> None:
        for name in ("test_c", "test_a", "test_b"):
            self._write_module(name, _PASS_MODULE)
        self._write_module("test_slowest", _sleep_module(1.0))
        self._commit("tie break fixture")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ranking = self._ranking(result.stdout)
        self.assertEqual(len(ranking), 4)
        self.assertEqual(ranking[0][1], "test_slowest")
        durations = [entry[2] for entry in ranking]
        self.assertEqual(durations, sorted(durations, reverse=True))
        # Whatever the fast modules measured, equal durations stay name-ordered.
        for earlier, later in zip(ranking, ranking[1:]):
            if earlier[2] == later[2]:
                self.assertLess(earlier[1], later[1])

    def test_twelve_modules_rank_exactly_the_slowest_ten(self) -> None:
        for index in range(12):
            self._write_module(f"test_m{index:02d}", _PASS_MODULE)
        self._commit("twelve module fixture")

        result = self._run()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        ranking = self._ranking(result.stdout)
        self.assertEqual(len(ranking), 10)
        self.assertEqual([entry[0] for entry in ranking], list(range(1, 11)))
        durations = [entry[2] for entry in ranking]
        self.assertEqual(durations, sorted(durations, reverse=True))
        self.assertIn("across 12 module(s)", result.stdout)
        self.assertIn("verify: OK", result.stdout)

    def test_totals_are_reported_on_success_and_failure(self) -> None:
        self._write_module("test_a", _PASS_MODULE)
        self._commit("success totals fixture")

        passing = self._run()

        self.assertEqual(passing.returncode, 0, passing.stdout + passing.stderr)
        self.assertRegex(passing.stdout,
                         r"unittest phase: \d+\.\d\ds across 1 module\(s\) "
                         r"on \d+ worker\(s\)")
        self.assertRegex(passing.stdout, r"verifier total: \d+\.\d\ds")

        self._write_module("test_b", _FAIL_MODULE)
        self._commit("failure totals fixture")

        failing = self._run()

        self.assertEqual(failing.returncode, 1)
        self.assertRegex(failing.stdout,
                         r"unittest phase: \d+\.\d\ds across 2 module\(s\) "
                         r"on \d+ worker\(s\)")
        self.assertRegex(failing.stdout, r"verifier total: \d+\.\d\ds")
        self.assertIn("verify: FAIL", failing.stdout)

    def test_ranking_reports_pass_and_fail_with_diagnostics(self) -> None:
        self._write_module("test_a", _PASS_MODULE)
        self._write_module("test_b", _FAIL_MODULE)
        self._commit("pass and fail ranking fixture")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        ranking = self._ranking(result.stdout)
        statuses = {entry[1]: entry[3] for entry in ranking}
        self.assertEqual(statuses, {"test_a": "PASS", "test_b": "FAIL"})
        self.assertIn("deliberate fixture failure marker",
                      result.stdout + result.stderr)
        self.assertIn("--- test_b output ---", result.stdout)

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

    def test_non_ascii_failure_survives_unittest_process_boundary(self) -> None:
        self._write_module("test_unicode", _UNICODE_FAIL_MODULE)
        self._commit("unicode failure fixture")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        combined = result.stdout + result.stderr
        for marker in ("繁體中文標準輸出", "繁體中文錯誤輸出", "繁體中文失敗診斷"):
            with self.subTest(marker=marker):
                self.assertIn(marker, combined)
        self.assertNotIn("\ufffd", combined)

    def test_invalid_native_bytes_fail_closed_at_unittest_boundary(self) -> None:
        self._write_module("test_invalid_bytes", _INVALID_BYTES_MODULE)
        self._commit("invalid verifier bytes fixture")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        combined = result.stdout + result.stderr
        self.assertIn("not valid UTF-8", combined)
        self.assertIn(r"\x80", combined)
        self.assertIn(r"\xff", combined)
        self.assertNotIn("\ufffd", combined)
        self.assertNotIn("UnicodeDecodeError", combined)
        self.assertNotIn("Traceback", combined)
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
        """Rewrite the fixture script with one configured project command."""
        template_text = TEMPLATE.read_text(encoding="utf-8")
        self.assertEqual(template_text.count(_UNCONFIGURED_PROJECT_BLOCK), 1)
        self.script.write_text(
            template_text.replace(
                _UNCONFIGURED_PROJECT_BLOCK, f"{call}\n"),
            encoding="utf-8")

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

    def test_invalid_native_bytes_fail_closed_at_run_boundary(self) -> None:
        self._write_bytes(
            "native_bytes.py",
            b"import os\n"
            b"os.write(1, b'native stdout ' + bytes([0x80]) + b'\\n')\n"
            b"os.write(2, b'native stderr ' + bytes([0xff]) + b'\\n')\n",
        )
        self._write_script_with_command(
            'run(sys.executable, "native_bytes.py")')
        self._commit("invalid native command fixture")

        result = self._run()

        self.assertEqual(result.returncode, 1)
        combined = result.stdout + result.stderr
        self.assertIn("not valid UTF-8", combined)
        self.assertIn(r"\x80", combined)
        self.assertIn(r"\xff", combined)
        self.assertNotIn("\ufffd", combined)
        self.assertNotIn("UnicodeDecodeError", combined)
        self.assertNotIn("Traceback", combined)
        self.assertNotIn("verify: OK", result.stdout)


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
