"""Unattended per-folder integration verification and its derived receipt."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.config import load_config
from assent.folder_verification import (RECEIPT_NAME, VerificationReceipt,
                                        read_receipt,
                                        receipt_matches_current_candidate,
                                        receipt_path, verify_folder,
                                        write_receipt)
from assent.gitops import (branch_tip, commit_of, folder_branches, tree_of,
                           working_tree_status)
from assent.verification_common import (ProvisionedLink, _require_no_overlap,
                                        provisioned_candidate_links, summary,
                                        union_worktree_links)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def make_directory_link(link: Path, target: Path) -> None:
    """Create a real directory link the way a human provisions one.

    Windows gets a genuine junction, which is what the observed source
    worktrees actually use and what an unattended run can create without an
    extra privilege; every other platform gets a directory symlink.  Nothing
    here is mocked, so the tests exercise the same path metadata production
    does.  Shared with ``tests.test_batch_verification``.
    """
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


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
        # Shared by trunk and every source branch, so a provisioned link stays
        # ignored in the source worktree and in the candidate alike.  The
        # tracked lib/ tree gives the nested cases a real parent chain: an
        # ignored directory link at lib/l10n/arb, an ignored generated leaf
        # beside a tracked source at lib/models/task.g.dart, and an ordinary
        # ignored cache directory nested in the same tree.
        (self.root / ".gitignore").write_text(
            "pkg/\nassets/\nignored/\nlib/l10n/arb/\n*.g.dart\n"
            "lib/.cache/\n", encoding="utf-8")
        (self.root / "lib" / "models").mkdir(parents=True)
        (self.root / "lib" / "l10n").mkdir(parents=True)
        (self.root / "lib" / "models" / "task.dart").write_text(
            "tracked source\n", encoding="utf-8")
        (self.root / "lib" / "l10n" / "app_en.arb").write_text(
            "{}\n", encoding="utf-8")
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

    def _link_target(self, name: str) -> Path:
        """Create an external directory holding one readable marker file."""
        target = self.parent / f"external {name}"
        target.mkdir(exist_ok=True)
        (target / "marker.txt").write_text(f"{name} marker\n", encoding="utf-8")
        return target

    def _peer_worktree(self) -> Path:
        """Add a second source worktree, so Git's ignore rules apply there too."""
        peer = self.source_worktree.parent / "peer plan"
        _git(self.root, "branch", "peer/run", self.target_tip)
        _git(self.root, "worktree", "add", str(peer), "peer/run")
        return peer

    def _provision_link(self, worktree: Path, name: str) -> Path:
        """Link ``worktree/name`` at a fresh external target and return it."""
        target = self._link_target(name)
        make_directory_link(worktree / name, target)
        return target

    def _write_verifier(self, exit_code: int, output_size: int = 0,
                        stderr: str = "", probe: Sequence[str] = (),
                        absent: Sequence[str] = (),
                        read: Sequence[str] = ()) -> None:
        """Write the stand-in full verifier.

        ``probe`` names directory paths whose ``marker.txt`` the verifier must
        be able to read from inside the candidate, ``read`` names files it must
        be able to read directly, and ``absent`` names paths that must not exist
        there; any expectation failing makes the run exit nonzero, which is
        exactly how a missing or unwanted candidate link should surface.
        """
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
            f"for name in {list(probe)!r}:\n"
            "    print('probe', name, (Path(name) / 'marker.txt')"
            ".read_text(encoding='utf-8').strip())\n"
            f"for name in {list(read)!r}:\n"
            "    print('read', name, Path(name)"
            ".read_text(encoding='utf-8').strip())\n"
            f"for name in {list(absent)!r}:\n"
            "    assert not Path(name).exists(), 'unexpected candidate path: ' + name\n"
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
                                stderr: str = "", probe: Sequence[str] = (),
                                absent: Sequence[str] = (),
                                read: Sequence[str] = ()) -> None:
        self._write_verifier(exit_code, output_size, stderr, probe, absent,
                             read)
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
        normalized = summary("short\r\noutput 診斷", "stderr\x00tail")
        self.assertEqual(
            normalized,
            "short\noutput 診斷\nstderr?tail")
        receipt = VerificationReceipt(
            version=1, status="FAILED", source_tip=self.source_tip,
            target_tip=self.target_tip,
            integration_tree=tree_of(self.root, self.source_tip),
            verify_script_sha256="a" * 64,
            verify_command="python .assent/verify.py", exit_code=1,
            completed_at="2026-01-01T00:00:00+00:00",
            failure_summary=normalized)
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
        with mock.patch("assent.verification_common.subprocess.run",
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


class TestFolderConflictDiagnostic(VerificationRepositoryCase):
    """A single folder that conflicts with the target is sent to reconcile."""

    def test_a_conflicting_source_names_reconcile_and_not_a_one_argument_rework(
            self) -> None:
        (self.source_worktree / "README.md").write_text(
            "from the source\n", encoding="utf-8")
        _git(self.source_worktree, "add", "README.md")
        _git(self.source_worktree, "commit", "-m", "source edits the shared file")
        (self.root / "README.md").write_text("from trunk\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "advance trunk")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_folder(self.cfg)
        text = output.getvalue()

        self.assertEqual(code, 1, text)
        self.assertIn("Integration conflict: README.md", text)
        self.assertIn("assent reconcile plan測試", text)
        self.assertNotIn("assent rework plan測試", text)
        receipt = read_receipt(receipt_path(self.cfg), self.root)
        self.assertEqual(receipt.status, "FAILED")


class TestProvisionedCandidateLinks(VerificationRepositoryCase):
    """Ignored root-level directory links follow the source into the candidate.

    A real junction on Windows and a real directory symlink elsewhere; nothing
    about the path metadata is mocked, because whether Git and the filesystem
    treat a junction as ignorable content is the fact under test.
    """

    def test_both_provisioned_links_are_readable_through_the_candidate(self):
        pkg = self._provision_link(self.source_worktree, "pkg")
        assets = self._provision_link(self.source_worktree, "assets")
        self._commit_target_verifier(exit_code=0, probe=("pkg", "assets"))

        self.assertEqual(verify_folder(self.cfg), 0)

        receipt = read_receipt(receipt_path(self.cfg), self.root)
        self.assertEqual(receipt.status, "PASSED")
        # The source worktree keeps its links, and both targets are untouched.
        self.assertTrue((pkg / "marker.txt").is_file())
        self.assertTrue((assets / "marker.txt").is_file())
        self.assertTrue((self.source_worktree / "pkg" / "marker.txt").is_file())
        self.assertTrue((self.source_worktree / "assets" / "marker.txt").is_file())
        branches, paths = self._temporary_resources()
        self.assertEqual((branches, paths), ([], []))

    def test_an_ordinary_ignored_directory_is_not_mirrored(self):
        self._provision_link(self.source_worktree, "pkg")
        ordinary = self.source_worktree / "ignored"
        ordinary.mkdir()
        (ordinary / "marker.txt").write_text("local only\n", encoding="utf-8")
        self._commit_target_verifier(
            exit_code=0, probe=("pkg",), absent=("ignored",))

        self.assertEqual(verify_folder(self.cfg), 0)

        self.assertEqual(
            read_receipt(receipt_path(self.cfg), self.root).status, "PASSED")
        self.assertTrue((ordinary / "marker.txt").is_file())

    def test_a_destination_git_does_not_ignore_is_skipped(self):
        # "local" is outside .gitignore, so mirroring it would change what the
        # candidate contains rather than restore what the source provisions.
        target = self._link_target("local")
        with provisioned_candidate_links(
                self.root, (ProvisionedLink("local", target),)) as mirrored:
            self.assertEqual(mirrored, ())
            self.assertFalse((self.root / "local").exists())

    def test_the_primitive_creates_and_then_removes_only_the_mirror(self):
        target = self._link_target("pkg")
        with provisioned_candidate_links(
                self.root, (ProvisionedLink("pkg", target),)) as mirrored:
            self.assertEqual([link.path for link in mirrored], ["pkg"])
            self.assertTrue((self.root / "pkg" / "marker.txt").is_file())
        self.assertFalse((self.root / "pkg").exists())
        self.assertTrue((target / "marker.txt").is_file())

    def test_two_worktrees_disagreeing_about_one_name_refuse(self):
        peer = self._peer_worktree()
        self._provision_link(self.source_worktree, "pkg")
        make_directory_link(peer / "pkg", self._link_target("other pkg"))

        self.assertEqual(
            union_worktree_links([self.source_worktree, self.source_worktree]),
            union_worktree_links([self.source_worktree]))
        with self.assertRaises(AssentError) as ctx:
            union_worktree_links([self.source_worktree, peer])
        self.assertIn("conflicting targets", str(ctx.exception))

    def test_an_occupied_destination_refuses_without_writing_evidence(self):
        # The candidate tracks "pkg" as a real directory; a link may add an
        # ignored path, never replace committed content.
        tracked = self.root / "pkg"
        tracked.mkdir()
        (tracked / "keep.txt").write_text("tracked\n", encoding="utf-8")
        _git(self.root, "add", "-f", "pkg/keep.txt")
        _git(self.root, "commit", "-m", "track a real pkg directory")
        self._provision_link(self.source_worktree, "pkg")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_folder(self.cfg)

        self.assertEqual(code, 1, output.getvalue())
        self.assertIn("already contains pkg", output.getvalue())
        self.assertFalse(receipt_path(self.cfg).exists())
        self.assertEqual(self.counter.exists(), False)

    def test_a_failing_verifier_preserves_the_target_and_the_source_link(self):
        pkg = self._provision_link(self.source_worktree, "pkg")
        self._commit_target_verifier(exit_code=3, probe=("pkg",))

        self.assertEqual(verify_folder(self.cfg), 1)

        self.assertEqual(
            read_receipt(receipt_path(self.cfg), self.root).status, "FAILED")
        self.assertTrue((pkg / "marker.txt").is_file())
        self.assertTrue((self.source_worktree / "pkg" / "marker.txt").is_file())
        branches, paths = self._temporary_resources()
        self.assertEqual((branches, paths), ([], []))

    def test_a_dangling_link_refuses_without_writing_evidence(self):
        target = self._provision_link(self.source_worktree, "pkg")
        shutil.rmtree(target)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_folder(self.cfg)

        # The human provisioned pkg deliberately, so a target that has gone
        # missing is a refusal rather than a candidate quietly missing it.
        self.assertEqual(code, 1, output.getvalue())
        self.assertIn("cannot be resolved", output.getvalue())
        self.assertFalse(receipt_path(self.cfg).exists())
        self.assertFalse(self.counter.exists())
        self.assertFalse((self.source_worktree / "pkg").is_dir())


class TestNestedAndFileProvisionedLinks(VerificationRepositoryCase):
    """Nested directory links and source-adjacent ignored leaf files.

    Everything here uses genuine filesystem links -- a junction on Windows, a
    directory symlink elsewhere, and an automatically created candidate file
    link -- because whether Git and the filesystem agree about a nested link is
    the fact under test.
    """

    def _provision_nested(self) -> Path:
        """Link the ignored lib/l10n/arb below tracked parents in the source."""
        target = self.parent / "external arb"
        target.mkdir()
        (target / "app_localizations.dart").write_text(
            "// generated localizations\n", encoding="utf-8")
        make_directory_link(self.source_worktree / "lib/l10n/arb", target)
        return target

    def _provision_generated_part(self) -> Path:
        """Write an ordinary ignored generated part beside its tracked source."""
        part = self.source_worktree / "lib/models/task.g.dart"
        part.write_text("// generated part\n", encoding="utf-8")
        return part

    def test_a_nested_directory_link_reaches_the_candidate_unchanged(self):
        target = self._provision_nested()
        self._commit_target_verifier(
            exit_code=0, read=("lib/l10n/arb/app_localizations.dart",))

        self.assertEqual(verify_folder(self.cfg), 0)

        self.assertEqual(
            read_receipt(receipt_path(self.cfg), self.root).status, "PASSED")
        # The source link and the external target both survive the run.
        self.assertTrue((target / "app_localizations.dart").is_file())
        self.assertTrue(
            (self.source_worktree / "lib/l10n/arb"
             / "app_localizations.dart").is_file())
        branches, paths = self._temporary_resources()
        self.assertEqual((branches, paths), ([], []))

    def test_an_ignored_generated_part_is_linked_without_preparation(self):
        part = self._provision_generated_part()
        self._commit_target_verifier(
            exit_code=0, read=("lib/models/task.g.dart",))

        # No hardlink twin and no symlink were prepared: the file is an
        # ordinary ignored file beside its tracked source.
        self.assertFalse(os.path.islink(part))
        self.assertEqual(verify_folder(self.cfg), 0)

        self.assertEqual(
            read_receipt(receipt_path(self.cfg), self.root).status, "PASSED")
        self.assertEqual(part.read_text(encoding="utf-8"), "// generated part\n")

    def test_ignored_trees_and_link_descendants_stay_out_of_the_candidate(self):
        self._provision_nested()
        self._provision_generated_part()
        cache = self.source_worktree / "lib/.cache"
        cache.mkdir()
        (cache / "build.g.dart").write_text("cached\n", encoding="utf-8")

        links = union_worktree_links([self.source_worktree])

        self.assertEqual(
            [(link.path, link.kind) for link in links],
            [("lib/l10n/arb", "directory"), ("lib/models/task.g.dart", "file")])
        self._commit_target_verifier(
            exit_code=0, read=("lib/models/task.g.dart",),
            absent=("lib/.cache",))
        self.assertEqual(verify_folder(self.cfg), 0)
        self.assertTrue((cache / "build.g.dart").is_file())

    def test_cleanup_removes_only_the_parents_assent_created(self):
        # lib/models is part of the candidate's tracked tree, so only a parent
        # chain Assent had to create may be removed again afterwards.
        target = self._link_target("nested pkg")
        link = ProvisionedLink("pkg/deep/nested", target)
        with provisioned_candidate_links(self.root, (link,)) as mirrored:
            self.assertEqual([entry.path for entry in mirrored],
                             ["pkg/deep/nested"])
            self.assertTrue((self.root / "pkg/deep/nested/marker.txt").is_file())
        self.assertFalse((self.root / "pkg").exists())
        self.assertTrue((self.root / "lib" / "models").is_dir())
        self.assertTrue((target / "marker.txt").is_file())

    def test_two_worktrees_offering_different_file_contents_refuse(self):
        peer = self._peer_worktree()
        self._provision_generated_part()
        (peer / "lib/models/task.g.dart").write_text(
            "// a different generation\n", encoding="utf-8")

        # Identical content at one path is one artifact, not a conflict.
        self.assertEqual(
            union_worktree_links([self.source_worktree, self.source_worktree]),
            union_worktree_links([self.source_worktree]))
        with self.assertRaises(AssentError) as ctx:
            union_worktree_links([self.source_worktree, peer])
        self.assertIn("differing contents", str(ctx.exception))

    def test_two_worktrees_disagreeing_about_a_kind_refuse(self):
        peer = self._peer_worktree()
        self._provision_generated_part()
        make_directory_link(peer / "lib/models/task.g.dart",
                            self._link_target("kind clash"))

        with self.assertRaises(AssentError) as ctx:
            union_worktree_links([self.source_worktree, peer])
        self.assertIn("as both a", str(ctx.exception))

    def test_one_provisioned_path_inside_another_refuses(self):
        # Discovery prunes a link's own descendants, so overlap can only arrive
        # from two worktrees; the union guard is what refuses it either way.
        with self.assertRaises(AssentError) as ctx:
            _require_no_overlap((
                ProvisionedLink("lib/l10n", self._link_target("outer")),
                ProvisionedLink("lib/l10n/arb", self._link_target("inner"))))
        self.assertIn("lies inside", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
