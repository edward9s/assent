"""Tests for the clean subcommand: verify every safety proof and action order against
a real Git repo."""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent import AssentError, gitops
from assent.clean import clean_folder, clean_folders
from assent.config import load_config
from assent.lockfile import hold_lock


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class TestClean(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".agents/\n", encoding="utf-8")
        (self.root / "README.md").write_text("init\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "init")

        self.folder = "plan01"
        self.tasks_dir = self.root / ".agents" / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.root / ".agents" / "agents.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.tasks_dir / "t001_task.e.toml").write_text(
            'status = "DONE"\n', encoding="utf-8")
        (self.tasks_dir / "agents.lock").write_text(
            'folder = "plan01"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.folder)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        shutil.rmtree(self.container, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _agents_snapshot(self) -> list[tuple[str, bool, bytes]]:
        agents_dir = self.root / ".agents"
        return [
            (str(path.relative_to(agents_dir)), path.is_dir(),
             b"" if path.is_dir() else path.read_bytes())
            for path in sorted(agents_dir.rglob("*"))
        ]

    def _run_clean(self) -> tuple[int, str]:
        before = self._agents_snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = clean_folder(self.cfg)
        self.assertEqual(self._agents_snapshot(), before)
        return code, output.getvalue()

    def _worktree_branch(self, commit: bool = False) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, self.folder)
        branch = gitops.ensure_branch(worktree, f"{self.folder}/")
        if commit:
            (worktree / "result.txt").write_text(branch, encoding="utf-8")
            gitops.commit_all(worktree, "finish result")
        return worktree, branch

    def test_clean_merged_worktree_and_all_prefixed_branches(self) -> None:
        worktree, branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", branch)
        _git(self.root, "branch", f"{self.folder}/older", "HEAD")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertFalse(self.container.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.folder}/"), [])
        self.assertIn("cleaned (worktree", output)
        self.assertIn(f"branch {branch}: cleaned", output)
        self.assertIn(f"branch {self.folder}/older: cleaned", output)

    def test_container_with_other_worktree_is_retained(self) -> None:
        worktree, branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", branch)
        other = gitops.ensure_worktree(self.root, "plan99")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertTrue(other.exists())
        self.assertTrue(self.container.exists())
        self.assertIn("cleaned (worktree", output)

    def test_leftover_empty_container_is_removed(self) -> None:
        """When the worktree has already been removed some other way and only an
        empty container remains, the entry-point cleanup removes it regardless of
        the skip outcome."""
        self.container.mkdir()

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertFalse(self.container.exists())
        self.assertIn("no worktree or branch to clean up", output)

    def test_dirty_worktree_is_retained(self) -> None:
        worktree, branch = self._worktree_branch()
        (worktree / "untracked.txt").write_text("do not discard\n", encoding="utf-8")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertIn("worktree not clean, retained", output)

    def test_unmerged_branch_retains_worktree_and_every_branch(self) -> None:
        merged = f"{self.folder}/merged"
        _git(self.root, "branch", merged, "HEAD")
        worktree, unmerged = self._worktree_branch(commit=True)

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertTrue(worktree.exists())
        branches = gitops.branches_with_prefix(self.root, f"{self.folder}/")
        self.assertEqual(branches, sorted([merged, unmerged]))
        self.assertIn(f"branch {unmerged}: skipped (not yet merged, retained)", output)
        self.assertIn(f"branch {merged}: skipped (another same-prefix branch is "
                      "not yet merged, retained)", output)

    def test_busy_lock_refuses_cleanup(self) -> None:
        worktree, branch = self._worktree_branch()
        with hold_lock(self.tasks_dir, self.folder):
            code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertIn("a run is in progress, refusing cleanup", output)

    def test_merged_branch_without_worktree_is_deleted(self) -> None:
        branch = f"{self.folder}/leftover"
        _git(self.root, "branch", branch, "HEAD")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertNotIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertIn("worktree does not exist", output)
        self.assertIn(f"branch {branch}: cleaned", output)

    def test_git_remove_failure_is_reported_and_returns_one(self) -> None:
        worktree, branch = self._worktree_branch()
        with patch("assent.clean.gitops.remove_worktree",
                   side_effect=AssentError("simulated Windows file lock")):
            code, output = self._run_clean()

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertIn("simulated Windows file lock", output)

    def test_git_failure_does_not_stop_later_folder(self) -> None:
        worktree, _ = self._worktree_branch()
        second = "plan02"
        second_dir = self.root / ".agents" / second
        second_dir.mkdir()
        (second_dir / "t001_task.e.toml").write_text(
            'status = "DONE"\n', encoding="utf-8")
        (second_dir / "agents.lock").write_text(
            'folder = "plan02"\n', encoding="utf-8")
        second_branch = f"{second}/leftover"
        _git(self.root, "branch", second_branch, "HEAD")
        second_cfg = load_config(self.config_path, second)
        before = self._agents_snapshot()

        output = io.StringIO()
        with patch("assent.clean.gitops.remove_worktree",
                   side_effect=AssentError("first item failed")), \
                contextlib.redirect_stdout(output):
            code = clean_folders([self.cfg, second_cfg])

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertNotIn(second_branch, gitops.branches_with_prefix(
            self.root, f"{second}/"))
        self.assertIn("first item failed", output.getvalue())
        self.assertIn(f"branch {second_branch}: cleaned", output.getvalue())
        self.assertEqual(self._agents_snapshot(), before)

    def test_missing_lock_fails_closed_without_creating_one(self) -> None:
        (self.tasks_dir / "agents.lock").unlink()
        branch = f"{self.folder}/leftover"
        _git(self.root, "branch", branch, "HEAD")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertFalse((self.tasks_dir / "agents.lock").exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertIn("without modifying .agents", output)

    def test_clean_detached_unmerged_head_is_retained(self) -> None:
        worktree = gitops.ensure_worktree(self.root, self.folder)
        (worktree / "detached_result.txt").write_text("do not discard\n", encoding="utf-8")
        gitops.commit_all(worktree, "detached result")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertTrue(worktree.exists())
        self.assertIn("worktree HEAD not yet merged, retained", output)


if __name__ == "__main__":
    unittest.main()
