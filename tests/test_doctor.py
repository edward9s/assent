"""Tests for assent doctor: machine-environment diagnosis without a project.

Adapter CLI probing is faked here (never assume claude/codex/agy are actually
installed on the test machine); git and the temp directory are exercised
through the real subprocess/tempfile calls, patched only for the failure
scenarios."""
import contextlib
import ctypes
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent.__main__ import main
from assent.doctor import (
    FAIL,
    PASS,
    WARN,
    _enable_windows_virtual_terminal,
    _print_check,
    doctor,
)


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


def _declines(prompt: str) -> str:
    """A confirm function that answers "no" without touching stdin.

    These exit-code tests run in whatever repository the suite was started
    from, which may legitimately hold orphaned temporary branches; without an
    injected answer doctor would put a real question to the terminal.
    """
    return "n"


class DoctorExitCodeTests(unittest.TestCase):
    def _run(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = doctor(confirm=_declines)
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
    @unittest.skipUnless(sys.platform == "win32", "Windows console API")
    def test_enables_ansi_processing_for_native_windows_console(self):
        import msvcrt

        calls = []

        class Kernel32:
            @staticmethod
            def GetConsoleMode(handle, mode):
                mode._obj.value = 0x0001
                return 1

            @staticmethod
            def SetConsoleMode(handle, mode):
                calls.append((handle.value, mode.value))
                return 1

        stream = StubStream("utf-8", True)
        stream.fileno = lambda: 7
        with patch.object(msvcrt, "get_osfhandle", return_value=123), \
                patch.object(ctypes, "WinDLL", return_value=Kernel32()):
            _enable_windows_virtual_terminal(stream)

        self.assertEqual(calls, [(123, 0x0005)])

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


def _fails_if_called(prompt: str) -> str:
    raise AssertionError(f"doctor asked a question it should not ask: {prompt}")


def _raises_eof(prompt: str) -> str:
    """Stand in for a closed stdin, the way reject's own tests already do."""
    raise EOFError


class DoctorOrphanBranchTests(unittest.TestCase):
    """Real repositories, real refs: the offer is proven by what Git holds
    afterwards, not by a mocked removal."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp()).resolve()
        self._git("init")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.root / "README.md").write_text("init\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", "init")
        self.target = self._git("branch", "--show-current").strip()
        self.worktrees: list[Path] = []
        # An ordinary folder branch, to prove the offer touches nothing else.
        self._git("branch", "demo/w1")

        old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old_cwd)
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for path in self.worktrees:
            subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                           cwd=self.root, capture_output=True, encoding="utf-8")
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True, encoding="utf-8")
        shutil.rmtree(self.root, ignore_errors=True)

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              encoding="utf-8", check=True).stdout

    def _published_orphan(self, branch: str) -> str:
        """A temporary branch whose tree a reachable commit already carries."""
        self._git("branch", branch, self.target)
        return branch

    def _superseded_orphan(self, branch: str) -> str:
        """A temporary branch carrying a tree no reachable commit has."""
        self._git("checkout", "-b", branch)
        (self.root / f"{branch.replace('/', '-')}.txt").write_text(
            "work\n", encoding="utf-8")
        self._git("add", "-A")
        self._git("commit", "-m", f"work on {branch}")
        self._git("checkout", self.target)
        return branch

    def _branches(self) -> set[str]:
        return {line[2:].strip()
                for line in self._git("branch", "--list").splitlines()
                if line.strip()}

    def _run(self, confirm):
        out = io.StringIO()
        with patch("assent.doctor.get_adapter",
                   side_effect=_fake_get_adapter(
                       {"claude": True, "codex": True, "antigravity": True})):
            with contextlib.redirect_stdout(out):
                code = doctor(confirm=confirm)
        return code, out.getvalue()

    def test_no_orphans_passes_and_asks_nothing(self):
        code, out = self._run(_fails_if_called)
        self.assertEqual(code, 0)
        self.assertIn("[OK] orphaned temporary branches: none", out)

    def test_orphans_warn_and_are_listed_in_order_with_classification(self):
        self._superseded_orphan("assent-reconcile/demo")
        self._published_orphan("assent-integration/batch/abc")
        code, out = self._run(_declines)

        self.assertEqual(code, 0)
        self.assertIn("[!] orphaned temporary branches: 2 found", out)
        listed = [line for line in out.splitlines()
                  if line.startswith("  assent-")]
        self.assertEqual(listed, [
            "  assent-integration/batch/abc: published",
            "  assent-reconcile/demo: superseded",
        ])

    def test_confirming_removes_exactly_the_offered_branches(self):
        self._published_orphan("assent-integration/batch/abc")
        self._superseded_orphan("assent-reconcile/demo")
        code, out = self._run(lambda prompt: "y")

        self.assertEqual(code, 0)
        remaining = self._branches()
        self.assertNotIn("assent-integration/batch/abc", remaining)
        self.assertNotIn("assent-reconcile/demo", remaining)
        self.assertIn("demo/w1", remaining)
        self.assertIn(self.target, remaining)
        self.assertIn("branch assent-reconcile/demo: removed", out)

    def test_every_declining_answer_removes_nothing(self):
        for answer in ("", "n", "N", "yes", "  ", _raises_eof):
            with self.subTest(answer=answer):
                self._published_orphan("assent-integration/batch/abc")
                self._superseded_orphan("assent-reconcile/demo")
                confirm = (answer if callable(answer)
                           else (lambda prompt, a=answer: a))
                code, out = self._run(confirm)

                self.assertEqual(code, 0)
                self.assertIn("declined: nothing was removed", out)
                remaining = self._branches()
                self.assertIn("assent-integration/batch/abc", remaining)
                self.assertIn("assent-reconcile/demo", remaining)
                self._git("branch", "-D", "assent-integration/batch/abc")
                self._git("branch", "-D", "assent-reconcile/demo")

    def test_checked_out_orphan_is_reported_and_never_offered(self):
        self._superseded_orphan("assent-reconcile/demo")
        path = self.root.parent / f"{self.root.name}.checkout"
        self._git("worktree", "add", str(path), "assent-reconcile/demo")
        self.worktrees.append(path)

        code, out = self._run(_fails_if_called)

        self.assertEqual(code, 0)
        self.assertIn("[!] orphaned temporary branches: 1 found", out)
        self.assertIn("assent-reconcile/demo: superseded, checked out in", out)
        self.assertIn("every listed branch is checked out in a worktree", out)
        self.assertIn("assent-reconcile/demo", self._branches())

    def test_checked_out_orphan_is_excluded_from_a_confirmed_removal(self):
        self._published_orphan("assent-integration/batch/abc")
        self._superseded_orphan("assent-reconcile/demo")
        path = self.root.parent / f"{self.root.name}.checkout"
        self._git("worktree", "add", str(path), "assent-reconcile/demo")
        self.worktrees.append(path)

        code, _ = self._run(lambda prompt: "y")

        self.assertEqual(code, 0)
        remaining = self._branches()
        self.assertNotIn("assent-integration/batch/abc", remaining)
        self.assertIn("assent-reconcile/demo", remaining)

    def test_confirmed_removal_performs_no_folder_operation(self):
        folder = self.root / ".assent" / "demo"
        folder.mkdir(parents=True)
        (folder / "t001_x.e.toml").write_text("status = \"DONE\"\n",
                                              encoding="utf-8")
        (folder / "_report.md").write_text("# report\n", encoding="utf-8")
        (folder / "_verification.toml").write_text("result = \"PASSED\"\n",
                                                   encoding="utf-8")
        before = {path.relative_to(self.root).as_posix():
                  path.read_bytes() for path in sorted(folder.iterdir())}
        self._published_orphan("assent-integration/batch/abc")

        code, _ = self._run(lambda prompt: "y")

        self.assertEqual(code, 0)
        after = {path.relative_to(self.root).as_posix():
                 path.read_bytes() for path in sorted(folder.iterdir())}
        self.assertEqual(after, before)
        self.assertNotIn("assent-integration/batch/abc", self._branches())
