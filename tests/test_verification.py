"""Unattended integration verification and derived receipt tests."""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import sys
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
    BATCH_RECEIPT_NAME, RECEIPT_NAME, BatchSource, BatchVerificationReceipt,
    VerificationReceipt, _summary, batch_receipt_is_current,
    batch_receipt_path, batch_receipt_staleness, build_batch_candidate,
    read_batch_receipt, read_receipt, receipt_matches_current_candidate,
    verifier_digest, verify_folder, write_batch_receipt, write_receipt,
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


class TestPackagedVerifier(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent verifier "))
        self.addCleanup(shutil.rmtree, self.parent, ignore_errors=True)
        self.root = self.parent / "candidate path with spaces"
        self.root.mkdir()
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)
        self._git_config()
        self.script = self.root / ".assent" / "verify.py"
        self.script.parent.mkdir()
        template = Path(__file__).parents[1] / "assent/templates/verify.py"
        self.script.write_text(template.read_text(encoding="utf-8"),
                               encoding="utf-8")

    def _git_config(self) -> None:
        for key, value in (("user.name", "Verifier Test"),
                           ("user.email", "verifier@example.invalid")):
            subprocess.run(["git", "config", key, value], cwd=self.root,
                           check=True, capture_output=True)

    def _commit(self, message: str) -> None:
        subprocess.run(["git", "add", "."], cwd=self.root, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-m", message], cwd=self.root,
                       check=True, capture_output=True)

    def _run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(self.script)], cwd=self.root,
                              capture_output=True, encoding="utf-8",
                              errors="replace")

    def test_committed_trailing_whitespace_fails_in_candidate_path_with_spaces(self):
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self._commit("root")
        (self.root / "changed.txt").write_text("bad \n", encoding="utf-8")
        self._commit("trailing whitespace")
        result = self._run()
        self.assertNotEqual(result.returncode, 0)

    def test_clean_committed_delta_and_root_commit_pass(self):
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        self._commit("root")
        self.assertEqual(self._run().returncode, 0)
        (self.root / "changed.txt").write_text("clean\n", encoding="utf-8")
        self._commit("clean delta")
        self.assertEqual(self._run().returncode, 0)

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


class TestArchivedUpstreamStack(VerificationRepositoryCase):
    """An archived upstream is proven by the roster, never by a live source."""

    def _commit_assent(self, message: str) -> None:
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", message)

    def _verify(self) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_folder(self.cfg)
        return code, output.getvalue()

    def test_archived_upstream_needs_no_source_and_keeps_the_receipt_fresh(self):
        upstream = self.assent_dir / "base"
        upstream.mkdir()
        (upstream / "t001_base.e.toml").write_text(
            'title = "Base"\n'
            'deps = []\nmodel = "core"\nstatus = "DONE"\n'
            'scope = ["result.txt"]\nverify = "python .assent/verify.py"\n'
            'goal = "done"\nacceptance = "verified"\n',
            encoding="utf-8")
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["base"]\nbase = "base"\n', encoding="utf-8")
        self._commit_assent("declare a live upstream")

        # A live upstream must still retain one clean, attached source identity.
        code, output = self._verify()
        self.assertEqual(code, 1)
        self.assertIn("has no base/* source branch", output)
        self.assertFalse((self.tasks_dir / RECEIPT_NAME).exists())

        # Archiving it deletes the live directory and the branch; the roster
        # entry then proves it complete and already merged into the target, so
        # no source resolution and neither ancestry check applies to it.
        shutil.rmtree(upstream)
        (self.assent_dir / "_archived.toml").write_text(
            "[[archived]]\n"
            'folder = "base"\n'
            'archived_at = "2026-01-01T00:00:00+00:00"\n',
            encoding="utf-8")
        self._commit_assent("archive the upstream")

        code, output = self._verify()

        self.assertEqual(code, 0, output)
        receipt = read_receipt(self.tasks_dir / RECEIPT_NAME, self.root)
        self.assertEqual(receipt.status, "PASSED")
        self.assertTrue(receipt_matches_current_candidate(self.cfg))

    def test_declared_base_allows_other_unaccepted_after_upstream(self):
        for folder in ("A", "B"):
            upstream = self.assent_dir / folder
            upstream.mkdir()
            (upstream / f"t001_{folder.lower()}.e.toml").write_text(
                f'title = "{folder}"\n'
                'deps = []\nmodel = "core"\nstatus = "DONE"\n'
                f'scope = ["{folder.lower()}.txt"]\n'
                'verify = "python .assent/verify.py"\n'
                'goal = "done"\nacceptance = "verified"\n',
                encoding="utf-8")
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["A", "B"]\nbase = "A"\n', encoding="utf-8")
        self._commit_assent("declare two live upstreams with base A")

        a_source = self.parent / f"{self.root.name}.worktrees" / "A"
        b_source = self.parent / f"{self.root.name}.worktrees" / "B"
        _git(self.root, "worktree", "add", "-b", "A/run", str(a_source))
        _git(self.root, "worktree", "add", "-b", "B/run", str(b_source))
        (a_source / "a.txt").write_text("A\n", encoding="utf-8")
        _git(a_source, "add", "a.txt")
        _git(a_source, "commit", "-m", "finish A")
        (b_source / "b.txt").write_text("B\n", encoding="utf-8")
        _git(b_source, "add", "b.txt")
        _git(b_source, "commit", "-m", "finish B")

        _git(self.source_worktree, "reset", "--hard", "A/run")
        (self.source_worktree / "downstream.txt").write_text(
            "downstream\n", encoding="utf-8")
        _git(self.source_worktree, "add", "downstream.txt")
        _git(self.source_worktree, "commit", "-m", "finish downstream")

        code, output = self._verify()
        self.assertEqual(code, 0, output)
        receipt = read_receipt(self.tasks_dir / RECEIPT_NAME, self.root)
        self.assertEqual(receipt.status, "PASSED")
        self.assertNotIn("stale stack", output)

    def test_multiple_unaccepted_after_upstreams_without_base_are_allowed(self):
        for folder in ("A", "B"):
            upstream = self.assent_dir / folder
            upstream.mkdir()
            (upstream / f"t001_{folder.lower()}.e.toml").write_text(
                f'title = "{folder}"\n'
                'deps = []\nmodel = "core"\nstatus = "DONE"\n'
                f'scope = ["{folder.lower()}.txt"]\n'
                'verify = "python .assent/verify.py"\n'
                'goal = "done"\nacceptance = "verified"\n',
                encoding="utf-8")
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["A", "B"]\n', encoding="utf-8")
        self._commit_assent("declare two ordering-only upstreams")

        a_source = self.parent / f"{self.root.name}.worktrees" / "A"
        b_source = self.parent / f"{self.root.name}.worktrees" / "B"
        _git(self.root, "worktree", "add", "-b", "A/run", str(a_source))
        _git(self.root, "worktree", "add", "-b", "B/run", str(b_source))
        (a_source / "a.txt").write_text("A\n", encoding="utf-8")
        _git(a_source, "add", "a.txt")
        _git(a_source, "commit", "-m", "finish A")
        (b_source / "b.txt").write_text("B\n", encoding="utf-8")
        _git(b_source, "add", "b.txt")
        _git(b_source, "commit", "-m", "finish B")

        _git(self.source_worktree, "reset", "--hard", "trunk")
        (self.source_worktree / "downstream.txt").write_text(
            "downstream\n", encoding="utf-8")
        _git(self.source_worktree, "add", "downstream.txt")
        _git(self.source_worktree, "commit", "-m", "finish downstream")

        code, output = self._verify()
        self.assertEqual(code, 0, output)
        receipt = read_receipt(self.tasks_dir / RECEIPT_NAME, self.root)
        self.assertEqual(receipt.status, "PASSED")
        self.assertNotIn(
            "cannot form one speculative verification candidate", output)


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


class BatchReceiptCase(VerificationRepositoryCase):
    """Two independent folders queued behind one full verification."""

    def setUp(self) -> None:
        super().setUp()
        self.second_worktree = self.source_worktree.parent / "plan貳"
        _git(self.root, "worktree", "add", "-b", "plan貳/run",
             str(self.second_worktree), self.target_tip)
        (self.second_worktree / "second.txt").write_text(
            "second result\n", encoding="utf-8")
        _git(self.second_worktree, "add", "second.txt")
        _git(self.second_worktree, "commit", "-m", "second result")
        self.second_tip = commit_of(self.root, "plan貳/run")
        self.order = (("plan測試", self.source_tip), ("plan貳", self.second_tip))

    def _batch_receipt(self, **overrides) -> BatchVerificationReceipt:
        """Build a receipt from the merge chain the folders currently produce."""
        candidate = build_batch_candidate(self.root, self.target_tip, self.order)
        self.assertTrue(candidate.ok, candidate.conflicts)
        sources = tuple(
            BatchSource(folder, tip, tree)
            for (folder, tip), tree in zip(self.order, candidate.step_trees))
        fields = dict(
            version=1, status="PASSED", target_tip=self.target_tip,
            sources=sources, final_tree=candidate.step_trees[-1],
            verify_script_sha256=verifier_digest(self.cfg),
            verify_command="python .assent/verify.py", exit_code=0,
            completed_at="2026-07-24T00:00:00+00:00", failure_summary="")
        fields.update(overrides)
        return BatchVerificationReceipt(**fields)

    def _write(self, receipt: BatchVerificationReceipt) -> Path:
        path = batch_receipt_path(self.assent_dir)
        write_batch_receipt(path, receipt, self.root)
        return path

    def _advance_trunk(self, name: str, content: str) -> None:
        (self.root / name).write_text(content, encoding="utf-8")
        _git(self.root, "add", name)
        _git(self.root, "commit", "-m", f"target {name}")

    def _drop_second_source(self) -> None:
        _git(self.root, "worktree", "remove", "--force", str(self.second_worktree))
        _git(self.root, "branch", "-D", "plan貳/run")


class TestBatchReceiptSchema(BatchReceiptCase):
    def test_round_trip_keeps_order_and_every_step_tree(self):
        receipt = self._batch_receipt()
        path = self._write(receipt)

        self.assertEqual(path, self.assent_dir / BATCH_RECEIPT_NAME)
        self.assertEqual(read_batch_receipt(path, self.root), receipt)
        self.assertEqual(receipt.folders, ("plan測試", "plan貳"))
        step_trees = [source.step_tree for source in receipt.sources]
        self.assertEqual(len(set(step_trees)), 2)
        self.assertEqual(receipt.final_tree, step_trees[-1])
        # The first step tree is the target with only the first folder merged.
        self.assertEqual(step_trees[0], tree_of(self.root, self.source_tip))
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([entry["folder"] for entry in data["sources"]],
                         ["plan測試", "plan貳"])

    def test_missing_unknown_and_inconsistent_content_fails_closed(self):
        receipt = self._batch_receipt()
        path = self._write(receipt)
        original = path.read_text(encoding="utf-8")
        first, second = receipt.sources
        sources_block = original[original.index("[[sources]]"):]
        bad_values = (
            original + 'unknown = "value"\n',
            original.replace('status = "PASSED"\n', ""),
            original.replace(f'target_tip = "{receipt.target_tip}"\n', ""),
            original.replace(sources_block, "sources = []\n"),
            original.replace(f'step_tree = "{second.step_tree}"\n', "", 1),
            original.replace(f'folder = "{second.folder}"',
                             f'folder = "{first.folder}"'),
            original.replace(f'source_tip = "{first.source_tip}"',
                             'source_tip = "abcd"'),
            original.replace(f'final_tree = "{receipt.final_tree}"',
                             f'final_tree = "{first.step_tree}"'),
            original.replace(f'step_tree = "{first.step_tree}"',
                             f'step_tree = "{first.source_tip}"'),
            original.replace(
                f'verify_script_sha256 = "{receipt.verify_script_sha256}"',
                'verify_script_sha256 = "xyz"'),
            original.replace("exit_code = 0", "exit_code = 3"),
            original.replace('completed_at = "2026-07-24T00:00:00+00:00"',
                             'completed_at = "2026-07-24T00:00:00"'),
            original.replace('folder = "plan貳"', 'folder = "../escape"'),
        )
        for text in bad_values:
            with self.subTest(text=text[-80:]):
                self.assertNotEqual(text, original)
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(AssentError):
                    read_batch_receipt(path, self.root)

    def test_empty_sources_and_wrong_types_are_rejected_before_writing(self):
        path = self.assent_dir / "unwritten.toml"
        for overrides in (
                {"sources": ()},
                {"version": True},
                {"status": "PASSING"},
                {"exit_code": True},
                {"verify_command": "python -m unittest"},
                {"failure_summary": "x" * 4001},
                {"status": "FAILED", "exit_code": 0},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(AssentError):
                    write_batch_receipt(
                        path, self._batch_receipt(**overrides), self.root)
                self.assertFalse(path.exists())

    def test_a_failed_batch_receipt_keeps_its_diagnosis(self):
        receipt = self._batch_receipt(
            status="FAILED", exit_code=7,
            failure_summary="Verification command failed: exit code 7")
        path = self._write(receipt)
        self.assertEqual(read_batch_receipt(path, self.root), receipt)
        self.assertFalse(batch_receipt_is_current(self.cfg, receipt))

    def test_single_folder_receipt_is_untouched_byte_for_byte(self):
        self.assertEqual(verify_folder(self.cfg), 0)
        folder_receipt = self.tasks_dir / RECEIPT_NAME
        before = folder_receipt.read_bytes()

        receipt = self._batch_receipt()
        path = self._write(receipt)
        self.assertEqual(read_batch_receipt(path, self.root), receipt)

        self.assertEqual(folder_receipt.read_bytes(), before)
        self.assertNotIn("sources", before.decode("utf-8"))
        self.assertFalse((self.tasks_dir / BATCH_RECEIPT_NAME).exists())
        self.assertEqual(read_receipt(folder_receipt, self.root).status, "PASSED")


class TestBatchReceiptStaleness(BatchReceiptCase):
    def test_unchanged_sources_and_verifier_stay_current(self):
        receipt = self._batch_receipt()
        self.assertEqual(batch_receipt_staleness(self.cfg, receipt), ())
        self.assertTrue(batch_receipt_is_current(self.cfg, receipt))

    def test_target_metadata_may_advance_but_content_may_not(self):
        receipt = self._batch_receipt()
        _git(self.root, "commit", "--allow-empty", "-m", "metadata only")
        self.assertEqual(batch_receipt_staleness(self.cfg, receipt), ())

        self._advance_trunk("target-only.txt", "target content\n")
        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("rebuilt step tree for plan測試", reasons[0])

    def test_a_moved_source_tip_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        (self.second_worktree / "third.txt").write_text("more", encoding="utf-8")
        _git(self.second_worktree, "add", "third.txt")
        _git(self.second_worktree, "commit", "-m", "second moved")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("source tip for plan貳 changed", reasons[0])
        self.assertFalse(batch_receipt_is_current(self.cfg, receipt))

    def test_a_source_accepted_on_its_own_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        _git(self.root, "merge", "--no-ff", "-m", "accept(plan貳)", "plan貳/run")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("plan貳 has already been accepted", reasons[0])

    def test_a_vanished_source_branch_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        self._drop_second_source()

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(
            reasons, ("source branch for plan貳 no longer exists",))

    def test_an_ambiguous_source_branch_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        _git(self.root, "branch", "plan貳/second", self.second_tip)

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("source branch for plan貳 is ambiguous", reasons[0])

    def test_a_changed_verifier_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        with open(self.assent_dir / "verify.py", "a", encoding="utf-8") as handle:
            handle.write("# changed verifier content\n")

        self.assertEqual(batch_receipt_staleness(self.cfg, receipt),
                         ("verification script changed",))

    def test_every_drifted_source_is_reported_together(self):
        receipt = self._batch_receipt()
        self._drop_second_source()
        (self.source_worktree / "extra.txt").write_text("extra", encoding="utf-8")
        _git(self.source_worktree, "add", "extra.txt")
        _git(self.source_worktree, "commit", "-m", "first moved")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 2, reasons)
        self.assertIn("source tip for plan測試 changed", reasons[0])
        self.assertEqual(reasons[1], "source branch for plan貳 no longer exists")

    def test_a_target_that_now_conflicts_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        self._advance_trunk("result.txt", "target owns this file\n")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("rebuilt integration of plan測試 conflicts", reasons[0])
        self.assertIn("result.txt", reasons[0])

    def test_rebuilding_leaves_no_temporary_branch_or_worktree(self):
        receipt = self._batch_receipt()
        self.assertEqual(batch_receipt_staleness(self.cfg, receipt), ())
        self.assertEqual(self._temporary_resources(), ([], []))


if __name__ == "__main__":
    unittest.main()
