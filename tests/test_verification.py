"""Unattended integration verification and derived receipt tests."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.config import load_config
from assent.gitops import (branch_tip, commit_of, folder_branches, tree_of,
                           working_tree_status)
from assent.verification import (
    RECEIPT_NAME, VerificationReceipt, _summary, read_receipt,
    receipt_matches_current_candidate, verify_folder, write_receipt,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class VerificationRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent verification 測試 "))
        self.root = self.parent / "repository with spaces"
        self.root.mkdir()
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Assent Test")
        _git(self.root, "config", "user.email", "assent@example.invalid")
        _git(self.root, "checkout", "-b", "trunk")
        self.counter = self.parent / "verify count.txt"
        self.observed = self.parent / "candidate facts.txt"
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / "plan測試"
        self.tasks_dir.mkdir(parents=True)
        (self.assent_dir / "assent.toml").write_text("", encoding="utf-8")
        self._write_verifier(exit_code=0)
        (self.tasks_dir / "t001_complete.e.toml").write_text(
            'title = "Complete"\n'
            'deps = []\nmodel = "core"\nstatus = "DONE"\n'
            'scope = ["result.txt"]\nverify = "python .assent/verify.py"\n'
            'goal = "done"\nacceptance = "verified"\n',
            encoding="utf-8")
        (self.root / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")
        self.target_tip = commit_of(self.root, "HEAD")

        _git(self.root, "branch", "plan測試/run", self.target_tip)
        self.source_worktree = (
            self.parent / f"{self.root.name}.worktrees" / "plan測試")
        self.source_worktree.parent.mkdir()
        _git(self.root, "worktree", "add", str(self.source_worktree),
             "plan測試/run")
        (self.source_worktree / "result.txt").write_text(
            "source result\n", encoding="utf-8")
        _git(self.source_worktree, "add", "result.txt")
        _git(self.source_worktree, "commit", "-m", "source result")
        self.source_tip = commit_of(self.root, "plan測試/run")
        self.cfg = load_config(self.assent_dir / "assent.toml", "plan測試")
        self.addCleanup(self._cleanup)

    def _write_verifier(self, exit_code: int, output_size: int = 0,
                        stderr: str = "") -> None:
        script = (
            "from pathlib import Path\n"
            "import subprocess\n"
            f"counter = Path({str(self.counter)!r})\n"
            f"observed = Path({str(self.observed)!r})\n"
            "count = int(counter.read_text() or '0') if counter.exists() else 0\n"
            "counter.write_text(str(count + 1), encoding='utf-8')\n"
            "parents = subprocess.run(['git', 'rev-list', '--parents', '-n', '1', "
            "'HEAD'], capture_output=True, text=True, check=True).stdout.strip()\n"
            "observed.write_text(str(Path.cwd()) + '\\n' + parents, encoding='utf-8')\n"
            "print('successful test noise begins')\n"
            f"print('x' * {output_size})\n"
            f"print({stderr!r}, file=__import__('sys').stderr)\n"
            f"raise SystemExit({exit_code})\n"
        )
        (self.assent_dir / "verify.py").write_text(script, encoding="utf-8")

    def _cleanup(self) -> None:
        if self.root.exists():
            listing = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=self.root,
                capture_output=True, encoding="utf-8", errors="replace")
            for line in listing.stdout.splitlines():
                if line.startswith("worktree "):
                    path = Path(line.removeprefix("worktree "))
                    if path.resolve() != self.root.resolve():
                        subprocess.run(
                            ["git", "worktree", "remove", "--force", str(path)],
                            cwd=self.root, capture_output=True)
        shutil.rmtree(self.parent, ignore_errors=True)

    def _commit_target_verifier(self, exit_code: int, output_size: int = 0,
                                stderr: str = "") -> None:
        self._write_verifier(exit_code, output_size, stderr)
        _git(self.root, "add", ".assent/verify.py")
        _git(self.root, "commit", "-m", "change full verifier")

    def _temporary_resources(self) -> tuple[list[str], list[Path]]:
        branches = folder_branches(self.root, "assent-integration")
        container = self.parent / f"{self.root.name}.integration"
        paths = list(container.iterdir()) if container.exists() else []
        return branches, paths


class TestVerificationRun(VerificationRepositoryCase):
    def test_full_verify_runs_once_in_two_parent_candidate_and_preserves_refs(self):
        target_before = commit_of(self.root, "trunk")
        source_before = branch_tip(self.root, "plan測試/run")

        self.assertEqual(verify_folder(self.cfg), 0)

        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")
        cwd, parents = self.observed.read_text(encoding="utf-8").splitlines()
        self.assertIn(f"{self.root.name}.integration", cwd)
        parent_ids = parents.split()
        self.assertEqual(parent_ids[1:], [target_before, source_before])
        receipt = read_receipt(self.tasks_dir / RECEIPT_NAME, self.root)
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.source_tip, source_before)
        self.assertEqual(receipt.target_tip, target_before)
        self.assertEqual(receipt.integration_tree, tree_of(self.root, source_before))
        self.assertEqual(commit_of(self.root, "trunk"), target_before)
        self.assertEqual(branch_tip(self.root, "plan測試/run"), source_before)
        self.assertTrue(working_tree_status(
            self.root, self.cfg.git_excludes).is_clean)
        self.assertEqual(self._temporary_resources(), ([], []))

    def test_nonzero_receipt_retains_actionable_tail_after_noisy_stdout(self):
        traceback = (
            "Traceback (most recent call last):\r\n"
            "  File 'verify.py', line 42, in <module>\r\n"
            "AssertionError: expected failure diagnosis\x00")
        self._commit_target_verifier(
            exit_code=7, output_size=5000, stderr=traceback)
        self.assertEqual(verify_folder(self.cfg), 1)
        receipt = read_receipt(self.tasks_dir / RECEIPT_NAME, self.root)
        self.assertEqual(receipt.status, "FAILED")
        self.assertEqual(receipt.exit_code, 7)
        self.assertLessEqual(len(receipt.failure_summary), 4000)
        self.assertTrue(receipt.failure_summary.startswith(
            "...[earlier output truncated]"))
        self.assertNotIn("successful test noise begins", receipt.failure_summary)
        self.assertIn("Traceback (most recent call last):", receipt.failure_summary)
        self.assertIn("AssertionError: expected failure diagnosis?", receipt.failure_summary)
        self.assertIn(
            "Verification command failed: python .assent/verify.py (exit code 7)",
            receipt.failure_summary)
        self.assertNotIn("\r", receipt.failure_summary)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")
        self.assertEqual(self._temporary_resources(), ([], []))

    def test_short_summary_is_unchanged_except_normalization(self):
        summary = _summary("short\r\noutput 診斷", "stderr\x00tail")
        self.assertEqual(
            summary,
            "short\noutput 診斷\nstderr?tail")
        receipt = VerificationReceipt(
            version=1, status="FAILED", source_tip=self.source_tip,
            target_tip=self.target_tip,
            integration_tree=tree_of(self.root, self.source_tip),
            verify_script_sha256="a" * 64,
            verify_command="python .assent/verify.py", exit_code=1,
            completed_at="2026-01-01T00:00:00+00:00",
            failure_summary=summary)
        path = self.tasks_dir / "normalized.toml"
        write_receipt(path, receipt, self.root)
        self.assertEqual(read_receipt(path, self.root), receipt)

    def test_conflict_never_runs_suite_or_leaves_passed_receipt(self):
        (self.source_worktree / "README.md").write_text(
            "source side\n", encoding="utf-8")
        _git(self.source_worktree, "add", "README.md")
        _git(self.source_worktree, "commit", "-m", "source conflict")
        (self.root / "README.md").write_text("target side\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "target conflict")

        self.assertEqual(verify_folder(self.cfg), 1)

        self.assertFalse(self.counter.exists())
        receipt = read_receipt(self.tasks_dir / RECEIPT_NAME, self.root)
        self.assertEqual(receipt.status, "FAILED")
        self.assertIn("README.md", receipt.failure_summary)
        self.assertEqual(self._temporary_resources(), ([], []))

    def test_incomplete_plan_refuses_before_candidate_and_invalidates_old_receipt(self):
        self.assertEqual(verify_folder(self.cfg), 0)
        task = self.tasks_dir / "t001_complete.e.toml"
        task.write_text(
            task.read_text(encoding="utf-8").replace(
                'status = "DONE"', 'status = "BLOCKED"'),
            encoding="utf-8")
        self.assertEqual(verify_folder(self.cfg), 1)
        self.assertFalse((self.tasks_dir / RECEIPT_NAME).exists())
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "1")

    def test_keyboard_interrupt_cleans_candidate_and_writes_no_receipt(self):
        with mock.patch("assent.verification.subprocess.run",
                        side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                verify_folder(self.cfg)
        self.assertFalse((self.tasks_dir / RECEIPT_NAME).exists())
        self.assertEqual(self._temporary_resources(), ([], []))

    def test_deleted_receipt_is_rebuilt(self):
        self.assertEqual(verify_folder(self.cfg), 0)
        (self.tasks_dir / RECEIPT_NAME).unlink()
        self.assertEqual(verify_folder(self.cfg), 0)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "2")


class TestReceiptMatching(VerificationRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(verify_folder(self.cfg), 0)

    def test_source_and_verifier_changes_are_stale(self):
        self.assertTrue(receipt_matches_current_candidate(self.cfg))
        (self.source_worktree / "another.txt").write_text("new", encoding="utf-8")
        _git(self.source_worktree, "add", "another.txt")
        _git(self.source_worktree, "commit", "-m", "source moved")
        self.assertFalse(receipt_matches_current_candidate(self.cfg))

        _git(self.source_worktree, "reset", "--hard", self.source_tip)
        self._write_verifier(exit_code=0)
        with open(self.assent_dir / "verify.py", "a", encoding="utf-8") as handle:
            handle.write("# changed verifier content\n")
        self.assertFalse(receipt_matches_current_candidate(self.cfg))

    def test_tree_identical_target_metadata_is_allowed_but_content_is_not(self):
        _git(self.root, "commit", "--allow-empty", "-m", "metadata only")
        self.assertTrue(receipt_matches_current_candidate(self.cfg))

        (self.root / "target-only.txt").write_text("different", encoding="utf-8")
        _git(self.root, "add", "target-only.txt")
        _git(self.root, "commit", "-m", "target content")
        self.assertFalse(receipt_matches_current_candidate(self.cfg))


class TestReceiptParsing(VerificationRepositoryCase):
    def test_round_trip_and_unknown_partial_or_wrong_object_fail_closed(self):
        self.assertEqual(verify_folder(self.cfg), 0)
        path = self.tasks_dir / RECEIPT_NAME
        receipt = read_receipt(path, self.root)
        copy = self.tasks_dir / "copy.toml"
        write_receipt(copy, receipt, self.root)
        self.assertEqual(read_receipt(copy, self.root), receipt)

        original = path.read_text(encoding="utf-8")
        bad_values = (
            original + 'unknown = "value"\n',
            'version = 1\nstatus = "PASSED"\n',
            original.replace(f'source_tip = "{receipt.source_tip}"',
                             'source_tip = "abcd"'),
            original.replace(f'integration_tree = "{receipt.integration_tree}"',
                             f'integration_tree = "{receipt.source_tip}"'),
            original.replace(
                f'verify_script_sha256 = "{receipt.verify_script_sha256}"',
                'verify_script_sha256 = "xyz"'),
        )
        for text in bad_values:
            with self.subTest(text=text[-80:]):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(AssentError):
                    read_receipt(path, self.root)

    def test_wrong_python_types_are_rejected_before_writing(self):
        bad = VerificationReceipt(
            version=True, status="PASSED", source_tip=self.source_tip,
            target_tip=self.target_tip, integration_tree=tree_of(
                self.root, self.source_tip), verify_script_sha256="a" * 64,
            verify_command="python .assent/verify.py", exit_code=0,
            completed_at="2026-01-01T00:00:00+00:00", failure_summary="")
        with self.assertRaises(AssentError):
            write_receipt(self.tasks_dir / "bad.toml", bad, self.root)


if __name__ == "__main__":
    unittest.main()
