"""Regressions for the isolated single-folder conflict reconciliation lifecycle."""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from assent import engine, gitops, verification
from assent import accept as accept_mod
from assent import batch_accept as batch_accept_mod
from assent import plan as plan_mod
from assent.config import load_config
from assent.reconcile import (reconcile_abort, reconcile_commit_message,
                              reconcile_continue, reconcile_start)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class ReconcileRepositoryCase(unittest.TestCase):
    """A repository whose folder source and integration target really conflict."""

    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent reconcile test "))
        self.root = self.parent / "repository with spaces"
        self.root.mkdir()
        self.addCleanup(self._cleanup)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Assent Test")
        _git(self.root, "config", "user.email", "assent@example.invalid")
        _git(self.root, "checkout", "-b", "trunk")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "shared.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")

        self.folder = "plan01"
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.assent_dir / "verify.py").write_text(
            "raise SystemExit('reconcile must never run the verifier')\n",
            encoding="utf-8")
        self._write_task()

    def _cleanup(self) -> None:
        if self.root.exists():
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=self.root,
                capture_output=True, encoding="utf-8", errors="replace")
            for line in result.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                path = Path(line.removeprefix("worktree "))
                if path.resolve() != self.root.resolve():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(path)],
                        cwd=self.root, capture_output=True)
        shutil.rmtree(self.parent, ignore_errors=True)

    # --- fixture helpers ---

    def _write_task(self, status: str = "DONE") -> Path:
        path = self.tasks_dir / "t001_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "prime"\n'
            f'status = "{status}"\n'
            'scope = ["shared.txt"]\n'
            'verify = "python .assent/verify.py"\n'
            'goal = "Complete the task."\n'
            'acceptance = "Verification passes."\n',
            encoding="utf-8")
        return path

    def _make_source(self, filename: str = "shared.txt",
                     content: str = "source\n") -> None:
        self.source_worktree = gitops.ensure_worktree(self.root, self.folder)
        self.source_branch = gitops.ensure_branch(
            self.source_worktree, f"{self.folder}/")
        (self.source_worktree / filename).write_text(content, encoding="utf-8")
        gitops.commit_all(self.source_worktree, "finish plan01")
        self.source_tip = gitops.branch_tip(self.root, self.source_branch)

    def _advance_target(self, content: str = "target\n") -> str:
        (self.root / "shared.txt").write_text(content, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "advance trunk")
        self.target_tip = gitops.commit_of(self.root, "HEAD")
        return self.target_tip

    def _conflicting_repository(self) -> None:
        """Source and target both rewrite ``shared.txt`` from the same base."""
        self._make_source()
        self._advance_target()

    def _config(self):
        return load_config(self.config_path, self.folder)

    def _managed_path(self) -> Path:
        return gitops.reconcile_worktree_path(self.root, self.folder)

    def _managed_branch(self) -> str:
        return f"assent-reconcile/{self.folder}"

    def _run(self, action) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = action(self._config())
        return code, buffer.getvalue()

    def _resolve(self, content: str = "resolved\n") -> None:
        (self._managed_path() / "shared.txt").write_text(
            content, encoding="utf-8")

    def _commit_reconcile_merge(self) -> str:
        """Simulate an interrupted run that had already created the merge commit."""
        path = self._managed_path()
        _git(path, "add", "-A")
        _git(path, "commit", "-m", reconcile_commit_message(self.folder))
        return gitops.commit_of(path, "HEAD")

    # --- assertions shared by several cases ---

    def _assert_target_untouched(self, tip: str) -> None:
        self.assertEqual(gitops.current_branch(self.root), "trunk")
        self.assertEqual(gitops.commit_of(self.root, "HEAD"), tip)
        self.assertTrue(gitops.working_tree_status(self.root).is_clean)

    def _assert_source_untouched(self) -> None:
        self.assertEqual(
            gitops.branch_tip(self.root, self.source_branch), self.source_tip)
        self.assertEqual(
            gitops.current_branch(self.source_worktree), self.source_branch)
        self.assertTrue(
            gitops.working_tree_status(self.source_worktree).is_clean)

    def _assert_managed_gone(self) -> None:
        self.assertFalse(self._managed_path().exists())
        self.assertFalse(
            gitops.branch_exists(self.root, self._managed_branch()))


class StartTest(ReconcileRepositoryCase):
    def test_start_prepares_only_the_reconciliation_worktree(self) -> None:
        self._conflicting_repository()
        code, output = self._run(reconcile_start)

        self.assertEqual(code, 0, output)
        path = self._managed_path()
        self.assertTrue(gitops.is_repo_worktree(self.root, path))
        self.assertEqual(gitops.current_branch(path), self._managed_branch())
        # Only the reconciliation worktree is in Git's merge state.
        self.assertEqual(gitops.merge_head(path), self.target_tip)
        self.assertEqual(gitops.commit_of(path, "HEAD"), self.source_tip)
        self.assertIsNone(gitops.merge_head(self.root))
        self.assertIsNone(gitops.merge_head(self.source_worktree))
        self.assertEqual(gitops.conflict_paths(path), ["shared.txt"])

        self._assert_target_untouched(self.target_tip)
        self._assert_source_untouched()
        self.assertIn(str(path), output)
        self.assertIn("shared.txt", output)

    def test_start_reports_a_stable_sorted_conflict_list(self) -> None:
        (self.root / "b.txt").write_text("base\n", encoding="utf-8")
        (self.root / "a.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "more files")

        self.source_worktree = gitops.ensure_worktree(self.root, self.folder)
        self.source_branch = gitops.ensure_branch(
            self.source_worktree, f"{self.folder}/")
        for name in ("a.txt", "b.txt", "shared.txt"):
            (self.source_worktree / name).write_text("source\n", encoding="utf-8")
        gitops.commit_all(self.source_worktree, "finish plan01")
        self.source_tip = gitops.branch_tip(self.root, self.source_branch)
        for name in ("a.txt", "b.txt", "shared.txt"):
            (self.root / name).write_text("target\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "advance trunk")
        self.target_tip = gitops.commit_of(self.root, "HEAD")

        code, output = self._run(reconcile_start)
        self.assertEqual(code, 0, output)
        self.assertEqual(gitops.conflict_paths(self._managed_path()),
                         ["a.txt", "b.txt", "shared.txt"])
        listed = [line.strip()[2:] for line in output.splitlines()
                  if line.strip().startswith("- ")]
        self.assertEqual(listed, ["a.txt", "b.txt", "shared.txt"])

    def test_start_without_a_real_conflict_reports_not_needed(self) -> None:
        self._make_source(filename="source-only.txt")
        self._advance_target()

        code, output = self._run(reconcile_start)

        self.assertEqual(code, 0, output)
        self.assertIn("not needed", output)
        self._assert_managed_gone()
        self._assert_target_untouched(self.target_tip)
        self._assert_source_untouched()

    def test_start_refuses_an_occupied_managed_path(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve("human edit\n")

        code, output = self._run(reconcile_start)

        self.assertEqual(code, 1)
        self.assertIn("occupied", output)
        # Nothing was deleted or overwritten.
        self.assertEqual(gitops.merge_head(self._managed_path()), self.target_tip)
        self.assertEqual(
            (self._managed_path() / "shared.txt").read_text(encoding="utf-8"),
            "human edit\n")

    def test_start_refuses_an_unfinished_folder(self) -> None:
        self._conflicting_repository()
        self._write_task(status="TODO")

        code, output = self._run(reconcile_start)

        self.assertEqual(code, 1)
        self.assertIn("not finished", output)
        self._assert_managed_gone()

    def test_start_refuses_a_dirty_integration_target(self) -> None:
        self._conflicting_repository()
        (self.root / "shared.txt").write_text("uncommitted\n", encoding="utf-8")

        code, output = self._run(reconcile_start)

        self.assertEqual(code, 1)
        self.assertIn("not clean", output)
        self._assert_managed_gone()

    def test_start_refuses_a_dirty_source_worktree(self) -> None:
        self._conflicting_repository()
        (self.source_worktree / "shared.txt").write_text(
            "uncommitted\n", encoding="utf-8")

        code, output = self._run(reconcile_start)

        self.assertEqual(code, 1)
        self.assertIn("not clean", output)
        self._assert_managed_gone()

    def test_start_reports_a_source_already_contained_in_the_target(self) -> None:
        self._make_source()
        _git(self.root, "merge", "--no-ff", "-m", "integrate",
             self.source_branch)
        target_tip = gitops.commit_of(self.root, "HEAD")

        code, output = self._run(reconcile_start)

        self.assertEqual(code, 0, output)
        self.assertIn("nothing to reconcile", output)
        self._assert_managed_gone()
        self._assert_target_untouched(target_tip)


class ContinueTest(ReconcileRepositoryCase):
    def test_continue_creates_the_merge_and_fast_forwards_only_the_source(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 0, output)
        merge = gitops.branch_tip(self.root, self.source_branch)
        self.assertEqual(gitops.commit_parents(self.root, merge),
                         (self.source_tip, self.target_tip))
        self.assertEqual(gitops.commit_message(self.root, merge).strip(),
                         reconcile_commit_message(self.folder).strip())
        # The resolved tree is preserved in the source worktree.
        self.assertEqual(
            (self.source_worktree / "shared.txt").read_text(encoding="utf-8"),
            "resolved\n")
        self.assertEqual(
            gitops.current_branch(self.source_worktree), self.source_branch)
        self.assertTrue(
            gitops.working_tree_status(self.source_worktree).is_clean)
        self._assert_managed_gone()
        self._assert_target_untouched(self.target_tip)
        self.assertEqual(
            (self.root / "shared.txt").read_text(encoding="utf-8"), "target\n")

    def test_continue_refuses_a_leftover_conflict_marker(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve("<<<<<<< HEAD\nsource\n=======\ntarget\n>>>>>>> trunk\n")

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 1)
        self.assertIn("--check", output)
        # No commit was made and the merge state, with the edit, is preserved.
        path = self._managed_path()
        self.assertEqual(gitops.merge_head(path), self.target_tip)
        self.assertEqual(gitops.commit_of(path, "HEAD"), self.source_tip)
        self._assert_source_untouched()
        self._assert_target_untouched(self.target_tip)

    def test_continue_refuses_an_edit_outside_the_conflict_scene(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        (self._managed_path() / "stray.txt").write_text("new\n", encoding="utf-8")

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 1)
        self.assertIn("stray.txt", output)
        self.assertEqual(gitops.merge_head(self._managed_path()), self.target_tip)
        self._assert_source_untouched()

    def test_continue_refuses_a_source_that_moved_independently(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        (self.source_worktree / "late.txt").write_text("late\n", encoding="utf-8")
        gitops.commit_all(self.source_worktree, "late source work")
        moved_tip = gitops.branch_tip(self.root, self.source_branch)

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 1)
        self.assertIn("moved independently", output)
        self.assertEqual(
            gitops.branch_tip(self.root, self.source_branch), moved_tip)
        path = self._managed_path()
        self.assertEqual(gitops.merge_head(path), self.target_tip)
        self.assertEqual((path / "shared.txt").read_text(encoding="utf-8"),
                         "resolved\n")

    def test_continue_resumes_after_the_merge_commit_was_already_created(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        merge = self._commit_reconcile_merge()

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 0, output)
        self.assertIn("no duplicate", output)
        self.assertEqual(
            gitops.branch_tip(self.root, self.source_branch), merge)
        self.assertEqual(gitops.commit_parents(self.root, merge),
                         (self.source_tip, self.target_tip))
        self._assert_managed_gone()
        self._assert_target_untouched(self.target_tip)

    def test_continue_resumes_after_the_source_was_already_fast_forwarded(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        merge = self._commit_reconcile_merge()
        gitops.fast_forward(self.source_worktree, merge)

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 0, output)
        self.assertIn("already", output)
        self.assertEqual(
            gitops.branch_tip(self.root, self.source_branch), merge)
        self.assertEqual(
            gitops.commit_history(self.root, merge)[0][0], merge)
        self._assert_managed_gone()

    def test_continue_resumes_when_only_branch_cleanup_remained(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        merge = self._commit_reconcile_merge()
        gitops.fast_forward(self.source_worktree, merge)
        gitops.remove_worktree(self.root, self._managed_path())

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 0, output)
        self.assertIn("only cleanup remained", output)
        self.assertEqual(
            gitops.branch_tip(self.root, self.source_branch), merge)
        self._assert_managed_gone()

    def test_continue_reports_target_drift_without_rewriting_the_merge(self) -> None:
        self._conflicting_repository()
        captured_target = self.target_tip
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        (self.root / "later.txt").write_text("later\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "target advances")
        advanced = gitops.commit_of(self.root, "HEAD")

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 0, output)
        self.assertIn("advanced from", output)
        merge = gitops.branch_tip(self.root, self.source_branch)
        self.assertEqual(gitops.commit_parents(self.root, merge),
                         (self.source_tip, captured_target))
        self._assert_target_untouched(advanced)

    def test_continue_refuses_a_managed_path_that_is_not_a_worktree(self) -> None:
        self._conflicting_repository()
        path = self._managed_path()
        path.mkdir(parents=True)
        (path / "foreign.txt").write_text("foreign\n", encoding="utf-8")

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 1)
        self.assertIn("not a worktree", output)
        self.assertTrue((path / "foreign.txt").exists())

    def test_continue_refuses_a_managed_worktree_on_a_foreign_branch(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        path = self._managed_path()
        _git(path, "merge", "--abort")
        _git(path, "checkout", "-b", "someone-else")

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 1)
        self.assertIn("someone-else", output)
        self.assertTrue(path.exists())
        self.assertTrue(gitops.branch_exists(self.root, self._managed_branch()))

    def test_continue_refuses_when_nothing_is_in_progress(self) -> None:
        self._conflicting_repository()

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 1)
        self.assertIn("no reconciliation is in progress", output)
        self._assert_source_untouched()
        self._assert_target_untouched(self.target_tip)


class AbortTest(ReconcileRepositoryCase):
    def test_abort_removes_only_the_managed_resources_and_is_idempotent(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve("half-finished\n")

        code, output = self._run(reconcile_abort)

        self.assertEqual(code, 0, output)
        self._assert_managed_gone()
        self._assert_source_untouched()
        self._assert_target_untouched(self.target_tip)

        code, output = self._run(reconcile_abort)
        self.assertEqual(code, 0, output)
        self.assertIn("nothing to abort", output)
        self._assert_source_untouched()
        self._assert_target_untouched(self.target_tip)

    def test_abort_after_the_source_moved_leaves_the_source_alone(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        merge = self._commit_reconcile_merge()
        gitops.fast_forward(self.source_worktree, merge)

        code, output = self._run(reconcile_abort)

        self.assertEqual(code, 0, output)
        self._assert_managed_gone()
        # The merge already fast-forwarded into the source survives untouched.
        self.assertEqual(
            gitops.branch_tip(self.root, self.source_branch), merge)
        self._assert_target_untouched(self.target_tip)

    def test_abort_refuses_a_managed_path_that_is_not_a_worktree(self) -> None:
        self._conflicting_repository()
        path = self._managed_path()
        path.mkdir(parents=True)
        (path / "foreign.txt").write_text("foreign\n", encoding="utf-8")

        code, output = self._run(reconcile_abort)

        self.assertEqual(code, 1)
        self.assertIn("not a worktree", output)
        self.assertTrue((path / "foreign.txt").exists())

    def test_abort_refuses_a_managed_worktree_on_a_foreign_branch(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        path = self._managed_path()
        _git(path, "merge", "--abort")
        _git(path, "checkout", "-b", "someone-else")

        code, output = self._run(reconcile_abort)

        self.assertEqual(code, 1)
        self.assertIn("someone-else", output)
        self.assertTrue(path.exists())
        self.assertTrue(gitops.branch_exists(self.root, self._managed_branch()))

    def test_abort_retains_a_worktree_holding_untracked_content(self) -> None:
        self._conflicting_repository()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        _git(self._managed_path(), "merge", "--abort")
        (self._managed_path() / "keep.txt").write_text("keep\n", encoding="utf-8")

        code, output = self._run(reconcile_abort)

        self.assertEqual(code, 1)
        self.assertIn("keep.txt", output)
        self.assertTrue((self._managed_path() / "keep.txt").exists())


class ReceiptInvalidationTest(ReconcileRepositoryCase):
    """Advancing the source expires every receipt written against the old one."""

    def _tree(self) -> str:
        return gitops.tree_of(self.root, "HEAD")

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _write_folder_receipt(self) -> Path:
        cfg = self._config()
        path = verification.receipt_path(cfg)
        verification.write_receipt(path, verification.VerificationReceipt(
            version=verification.RECEIPT_VERSION, status="PASSED",
            source_tip=self.source_tip, target_tip=self.target_tip,
            integration_tree=self._tree(),
            verify_script_sha256=verification.verifier_digest(cfg),
            verify_command=verification.VERIFY_COMMAND, exit_code=0,
            completed_at=self._now(), failure_summary=""), self.root)
        return path

    def _write_batch_receipt(self, *folders: tuple[str, str]) -> Path:
        path = verification.batch_receipt_path(self.assent_dir)
        tree = self._tree()
        verification.write_batch_receipt(path, verification.BatchVerificationReceipt(
            version=verification.BATCH_RECEIPT_VERSION, status="PASSED",
            target_tip=self.target_tip,
            sources=tuple(verification.BatchSource(folder, tip, tree)
                          for folder, tip in folders),
            final_tree=tree,
            verify_script_sha256=verification.verifier_digest(self._config()),
            verify_command=verification.VERIFY_COMMAND, exit_code=0,
            completed_at=self._now(), failure_summary=""), self.root)
        return path

    def _make_peer_source(self, folder: str = "peer01") -> str:
        """A second finished folder whose own source identity does not move."""
        worktree = gitops.ensure_worktree(self.root, folder)
        branch = gitops.ensure_branch(worktree, f"{folder}/")
        (worktree / f"{folder}.txt").write_text("peer\n", encoding="utf-8")
        gitops.commit_all(worktree, f"finish {folder}")
        return gitops.branch_tip(self.root, branch)

    def _resolved_continue(self) -> tuple[int, str]:
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        return self._run(reconcile_continue)

    def test_continue_deletes_the_stale_folder_receipt(self) -> None:
        self._conflicting_repository()
        receipt = self._write_folder_receipt()

        code, output = self._resolved_continue()

        self.assertEqual(code, 0, output)
        self.assertFalse(receipt.exists())
        self.assertIn("stale verification receipt deleted", output)

        # The deleted evidence is exactly what accept refuses to do without.
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            accepted = accept_mod.accept_folder(self._config())
        self.assertEqual(accepted, 1, buffer.getvalue())
        self.assertIn("assent verify", buffer.getvalue())

    def test_continue_invalidates_a_batch_receipt_that_records_this_folder(self
                                                                          ) -> None:
        self._conflicting_repository()
        peer_tip = self._make_peer_source()
        batch = self._write_batch_receipt(
            (self.folder, self.source_tip), ("peer01", peer_tip))

        code, output = self._resolved_continue()

        self.assertEqual(code, 0, output)
        self.assertFalse(batch.exists())
        self.assertIn("stale batch verification receipt deleted", output)
        self.assertIn(self.folder, output)

    def test_continue_keeps_a_batch_receipt_whose_sources_are_all_current(self
                                                                         ) -> None:
        self._conflicting_repository()
        peer_tip = self._make_peer_source()
        batch = self._write_batch_receipt(("peer01", peer_tip))

        code, output = self._resolved_continue()

        self.assertEqual(code, 0, output)
        self.assertTrue(batch.exists())
        self.assertNotIn("batch verification receipt deleted", output)
        self.assertEqual(
            verification.read_batch_receipt(batch, self.root).folders, ("peer01",))

    def test_start_and_abort_leave_every_receipt_in_place(self) -> None:
        self._conflicting_repository()
        receipt = self._write_folder_receipt()
        batch = self._write_batch_receipt((self.folder, self.source_tip))

        self.assertEqual(self._run(reconcile_start)[0], 0)
        self.assertTrue(receipt.exists())
        self.assertTrue(batch.exists())

        self.assertEqual(self._run(reconcile_abort)[0], 0)
        self.assertTrue(receipt.exists())
        self.assertTrue(batch.exists())

    def test_an_unreadable_batch_receipt_is_reported_and_kept(self) -> None:
        self._conflicting_repository()
        batch = verification.batch_receipt_path(self.assent_dir)
        batch.write_text("not a batch receipt\n", encoding="utf-8")

        code, output = self._resolved_continue()

        self.assertEqual(code, 0, output)
        self.assertTrue(batch.exists())
        self.assertIn("cannot be read", output)

    def test_continue_states_that_no_verification_ran_and_names_both_steps(self
                                                                          ) -> None:
        self._conflicting_repository()

        code, output = self._resolved_continue()

        self.assertEqual(code, 0, output)
        self.assertIn("no verification has run", output)
        self.assertIn("neither the focused task tests nor the complete "
                      "verification", output)
        self.assertIn(f"assent verify {self.folder}", output)
        self.assertIn(f"assent accept {self.folder}", output)
        # The invalid one-argument rework command is never suggested.
        self.assertNotIn(f"assent rework {self.folder}", output)

    def test_a_resumed_continue_reports_the_same_boundary_and_receipts(self) -> None:
        self._conflicting_repository()
        receipt = self._write_folder_receipt()
        self.assertEqual(self._run(reconcile_start)[0], 0)
        self._resolve()
        merge = self._commit_reconcile_merge()
        gitops.fast_forward(self.source_worktree, merge)
        gitops.remove_worktree(self.root, self._managed_path())

        code, output = self._run(reconcile_continue)

        self.assertEqual(code, 0, output)
        self.assertIn("only cleanup remained", output)
        self.assertFalse(receipt.exists())
        self.assertIn("no verification has run", output)
        self.assertIn(f"assent accept {self.folder}", output)


class LifecycleBoundaryTest(ReconcileRepositoryCase):
    def test_reconcile_never_verifies_accepts_or_edits_a_task_status(self) -> None:
        self._conflicting_repository()
        task_file = self.tasks_dir / "t001_task.e.toml"
        before = task_file.read_bytes()
        target_before = self.target_tip

        def _forbidden(*args, **kwargs):
            raise AssertionError("the reconciliation lifecycle started this")

        with patch.object(verification, "verify_folder", _forbidden), \
                patch.object(verification, "verify_folder_if_needed", _forbidden), \
                patch.object(verification, "_run_full_verifier", _forbidden), \
                patch.object(verification, "verify_batch", _forbidden), \
                patch.object(accept_mod, "accept_folder", _forbidden), \
                patch.object(batch_accept_mod, "accept_all", _forbidden), \
                patch.object(engine, "run", _forbidden), \
                patch.object(engine, "_run_verify", _forbidden), \
                patch.object(engine, "_run_verify_quiet", _forbidden), \
                patch.object(plan_mod, "set_status", _forbidden):
            self.assertEqual(self._run(reconcile_start)[0], 0)
            self._resolve()
            self.assertEqual(self._run(reconcile_continue)[0], 0)

        self.assertEqual(task_file.read_bytes(), before)
        # The target branch never moved and never entered a merge state.
        self._assert_target_untouched(target_before)
        self.assertFalse(
            (self.assent_dir / self.folder / "_verification.toml").exists())

    def test_a_busy_folder_lock_refuses_without_touching_anything(self) -> None:
        from assent.lockfile import hold_lock

        self._conflicting_repository()
        with hold_lock(self.tasks_dir, self.folder):
            code, output = self._run(reconcile_start)

        self.assertEqual(code, 1)
        self.assertIn("refused", output)
        self._assert_managed_gone()
        self._assert_source_untouched()
        self._assert_target_untouched(self.target_tip)


if __name__ == "__main__":
    unittest.main()
