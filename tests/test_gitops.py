"""gitops tests: all run inside a temporary repo created by tempfile.mkdtemp() (Rule 4)."""
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from assent import AssentError
from assent.gitops import (
    branches_with_prefix, changes_outside_scope, commit_all, commit_if_dirty,
    ensure_branch,
    ensure_clean, ensure_worktree, head_ref, restore, tracked_paths,
    worktree_path)


def _run(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True,
                   encoding="utf-8", check=True)


class GitTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        _run(self.root, "init")
        _run(self.root, "config", "user.name", "Test")
        _run(self.root, "config", "user.email", "test@example.com")
        (self.root / "README.md").write_text("init\n", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "init")

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


class TestEnsureClean(GitTestCase):
    def test_clean_repo_passes(self):
        ensure_clean(self.root)  # should not raise

    def test_dirty_repo_raises(self):
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(AssentError):
            ensure_clean(self.root)

    def test_modified_tracked_file_raises(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        with self.assertRaises(AssentError):
            ensure_clean(self.root)

    def test_excluded_runtime_artifacts_are_ignored(self):
        # .assent needs an already-tracked file first, or porcelain lists the whole
        # untracked directory instead of individual files.
        (self.root / ".assent").mkdir()
        (self.root / ".assent" / "assent.toml").write_text("x", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "track management dir")
        (self.root / ".assent" / "assent.log").write_text("live", encoding="utf-8")
        ensure_clean(self.root, excludes=(".assent/assent.log",))

    def test_without_excludes_runtime_log_counts_as_dirty(self):
        (self.root / "assent.log").write_text("live", encoding="utf-8")
        with self.assertRaises(AssentError):
            ensure_clean(self.root)


class TestEnsureBranch(GitTestCase):
    def test_creates_new_branch_from_main(self):
        branch = ensure_branch(self.root, "workflow/")
        self.assertTrue(branch.startswith("workflow/"))
        current = subprocess.run(
            ["git", "branch", "--show-current"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(current, branch)

    def test_reuses_existing_workflow_branch(self):
        first = ensure_branch(self.root, "workflow/")
        second = ensure_branch(self.root, "workflow/")
        self.assertEqual(first, second)

    def test_run_id_unique_across_prefixless_calls(self):
        # Starting from a non-workflow branch, two calls (switching back first) should
        # each create their own branch.
        branch1 = ensure_branch(self.root, "workflow/")
        _run(self.root, "checkout", "master")
        branch2 = ensure_branch(self.root, "workflow/")
        self.assertNotEqual(branch1, branch2)

    def test_branch_prefix_is_literal_not_git_pattern(self):
        _run(self.root, "branch", "workflow/one", "HEAD")
        _run(self.root, "branch", "worker/two", "HEAD")
        self.assertEqual(branches_with_prefix(self.root, "work*"), [])
        self.assertEqual(branches_with_prefix(self.root, "workflow/"),
                         ["workflow/one"])


class TestEnsureWorktree(GitTestCase):
    def tearDown(self) -> None:
        container = self.root.parent / f"{self.root.name}.worktrees"
        if container.exists():
            for path in container.iterdir():
                if (path / ".git").is_file():
                    _run(self.root, "worktree", "remove", "--force", str(path))
                else:
                    shutil.rmtree(path)
            container.rmdir()
        _run(self.root, "worktree", "prune")
        super().tearDown()

    def test_worktree_path_is_beside_main_tree(self):
        expect = self.root.parent / f"{self.root.name}.worktrees" / "parallel01"
        self.assertEqual(worktree_path(self.root, "parallel01"), expect)

    def test_creates_and_idempotently_reuses_worktree(self):
        first = ensure_worktree(self.root, "parallel01")
        second = ensure_worktree(self.root, "parallel01")
        self.assertEqual(first, second)
        self.assertTrue((first / ".git").is_file())
        branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=first,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(branch, "")

        listed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        listed_paths = [Path(line.removeprefix("worktree ")).resolve()
                        for line in listed.splitlines()
                        if line.startswith("worktree ")]
        self.assertEqual(listed_paths.count(first.resolve()), 1)

    def test_prunes_stale_metadata_and_recreates_deleted_worktree(self):
        path = ensure_worktree(self.root, "parallel01")
        shutil.rmtree(path)

        rebuilt = ensure_worktree(self.root, "parallel01")
        self.assertEqual(rebuilt, path)
        self.assertTrue((rebuilt / ".git").is_file())

    def test_existing_non_worktree_directory_raises(self):
        path = worktree_path(self.root, "parallel01")
        path.mkdir(parents=True)
        (path / "keep.txt").write_text("do not overwrite\n", encoding="utf-8")

        with self.assertRaisesRegex(AssentError, "not a valid worktree of this repo"):
            ensure_worktree(self.root, "parallel01")
        self.assertEqual((path / "keep.txt").read_text(encoding="utf-8"),
                         "do not overwrite\n")

    def test_branch_and_commit_do_not_affect_main_worktree(self):
        main_branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        path = ensure_worktree(self.root, "parallel01")

        branch = ensure_branch(path, "parallel01/")
        (path / "README.md").write_text("worktree\n", encoding="utf-8")
        commit_all(path, "test: worktree commit")

        current_main = subprocess.run(
            ["git", "branch", "--show-current"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertTrue(branch.startswith("parallel01/"))
        self.assertEqual(current_main, main_branch)
        self.assertEqual((self.root / "README.md").read_text(encoding="utf-8"),
                         "init\n")


class TestTrackedPaths(GitTestCase):
    def test_exact_file_and_directory_queries(self):
        folder = self.root / ".assent" / "plan01"
        folder.mkdir(parents=True)
        task = folder / "t001_task.e.toml"
        task.write_text("status = \"TODO\"\n", encoding="utf-8")
        _run(self.root, "add", str(task.relative_to(self.root)))

        self.assertEqual(tracked_paths(self.root, ".assent/plan01"),
                         [".assent/plan01/t001_task.e.toml"])
        self.assertEqual(tracked_paths(
            self.root, ".assent/plan01", ref="HEAD"), [])

        _run(self.root, "commit", "-m", "track task")
        self.assertEqual(tracked_paths(
            self.root, ".assent/plan01", ref="HEAD"),
            [".assent/plan01/t001_task.e.toml"])


class TestChangesOutsideScope(GitTestCase):
    def test_excluded_paths_are_never_a_scope_violation(self):
        (self.root / "assent.log").write_text("AI output", encoding="utf-8")
        self.assertEqual(
            changes_outside_scope(self.root, [], excludes=("assent.log",)), [])

    def test_new_file_in_scope_not_flagged(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "t.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertEqual(outside, [])

    def test_new_file_outside_scope_flagged(self):
        (self.root / "secret.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertIn("secret.py", outside)

    def test_modified_file_outside_scope_flagged(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertIn("README.md", outside)

    def test_modified_file_inside_scope_not_flagged(self):
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["README.md"])
        self.assertEqual(outside, [])

    def test_windows_backslash_scope_normalized(self):
        (self.root / "workflow").mkdir()
        (self.root / "workflow" / "gitops.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["workflow\\"])
        self.assertEqual(outside, [])

    def test_empty_scope_denies_all(self):
        # Task file has no workflow: scope entry -> scope=[] -> fail-closed, every change
        # counts as out of scope
        (self.root / "anything.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, [])
        self.assertIn("anything.py", outside)

    def test_non_ascii_filename_not_octal_escaped(self):
        # core.quotepath=false: Chinese filenames should show directly as UTF-8, not \NNN
        # octal-escaped
        (self.root / "測試.py").write_text("x", encoding="utf-8")
        outside = changes_outside_scope(self.root, ["tests/"])
        self.assertIn("測試.py", outside)

    def test_since_ref_covers_committed_wip_changes(self):
        # After a quota-interruption wip checkpoint commits the out-of-scope file, the
        # working tree is clean; only since_ref lets committed out-of-scope changes "since
        # the task started" be caught too.
        start = head_ref(self.root)
        (self.root / "secret.py").write_text("x", encoding="utf-8")
        commit_all(self.root, "wip(T1): quota interrupted, progress preserved")
        self.assertEqual(changes_outside_scope(self.root, ["tests/"]), [])
        outside = changes_outside_scope(self.root, ["tests/"], since_ref=start)
        self.assertIn("secret.py", outside)

    def test_since_ref_deduplicates_with_working_tree(self):
        start = head_ref(self.root)
        (self.root / "secret.py").write_text("x", encoding="utf-8")
        commit_all(self.root, "wip: preserved")
        (self.root / "secret.py").write_text("y", encoding="utf-8")  # changed the same file again
        outside = changes_outside_scope(self.root, ["tests/"], since_ref=start)
        self.assertEqual(outside.count("secret.py"), 1)


class TestCommitAll(GitTestCase):
    def test_commit_all_cleans_working_tree(self):
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(W2): test commit")
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertEqual(status.strip(), "")

    def test_commit_message_recorded(self):
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(W2): message check")
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(log, "auto(W2): message check")

    def test_excludes_inside_gitignored_dir_do_not_crash(self):
        # Regression: when the whole .assent/ is gitignored, naming an ignored path in the
        # exclude pathspec makes git add exit 1 (found via dogfooding); filtering must let
        # commit succeed normally.
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        assent_dir = self.root / ".assent"
        assent_dir.mkdir()
        (assent_dir / "assent.log").write_text("log", encoding="utf-8")
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(t001): must not fail due to an ignored exclude entry",
                   excludes=(".assent/assent.log", ".assent/plan01/_report.md"))
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertEqual(status.strip(), "")

    def test_live_excludes_still_excluded_when_mixed_with_ignored(self):
        # Mixed case: one entry is gitignored (filtered out), one is not ignored (must
        # still take effect as an exclude)
        (self.root / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
        (self.root / "ignored.log").write_text("x", encoding="utf-8")
        (self.root / "live.log").write_text("x", encoding="utf-8")
        (self.root / "new.txt").write_text("x", encoding="utf-8")
        commit_all(self.root, "auto(t001): mixed excludes",
                   excludes=("ignored.log", "live.log"))
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertIn("new.txt", tracked)
        self.assertNotIn("live.log", tracked)   # exclude entry not covered by ignore still applies
        self.assertNotIn("ignored.log", tracked)

    def test_embedded_repo_without_commit_is_skipped(self):
        nested = self.root / ".test-tmp" / "probe" / "inner"
        nested.mkdir(parents=True)
        _run(nested, "init")
        (nested / "orphan.txt").write_text("uncommitted\n", encoding="utf-8")
        (self.root / "normal.txt").write_text("normal change\n", encoding="utf-8")

        output = StringIO()
        with redirect_stdout(output):
            commit_all(self.root, "auto(t010): skip embedded repo")

        tracked = subprocess.run(
            ["git", "ls-files"], cwd=self.root, capture_output=True,
            encoding="utf-8", check=True).stdout.splitlines()
        self.assertIn("normal.txt", tracked)
        self.assertFalse(any(path.startswith(".test-tmp/") for path in tracked))
        self.assertIn("warning: skipped embedded repo: .test-tmp/probe/inner, handle it manually",
                      output.getvalue())


class TestCommitIfDirty(GitTestCase):
    def test_dirty_tree_commits_and_returns_true(self):
        (self.root / "wip.txt").write_text("x", encoding="utf-8")
        self.assertTrue(commit_if_dirty(self.root, "wip(T1): quota interrupted, progress preserved"))
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(log, "wip(T1): quota interrupted, progress preserved")

    def test_clean_tree_returns_false_without_commit(self):
        before = head_ref(self.root)
        self.assertFalse(commit_if_dirty(self.root, "should not appear"))
        self.assertEqual(head_ref(self.root), before)

    def test_excluded_artifact_alone_does_not_create_commit(self):
        before = head_ref(self.root)
        (self.root / "assent.log").write_text("AI output", encoding="utf-8")
        self.assertFalse(commit_if_dirty(self.root, "should not exist",
                                         excludes=("assent.log",)))
        self.assertEqual(head_ref(self.root), before)


class TestHeadRef(GitTestCase):
    def test_returns_current_head_hash(self):
        ref = head_ref(self.root)
        expect = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.assertEqual(ref, expect)

    def test_repo_without_commits_returns_none(self):
        import tempfile
        empty = Path(tempfile.mkdtemp())
        try:
            _run(empty, "init")
            self.assertIsNone(head_ref(empty))
        finally:
            shutil.rmtree(empty, ignore_errors=True)


class TestRestore(GitTestCase):
    def test_restore_removes_untracked_and_modified(self):
        (self.root / "untracked.txt").write_text("x", encoding="utf-8")
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        restore(self.root)
        self.assertFalse((self.root / "untracked.txt").exists())
        self.assertEqual((self.root / "README.md").read_text(encoding="utf-8"), "init\n")
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout
        self.assertEqual(status.strip(), "")


class TestGitMissing(unittest.TestCase):
    def test_missing_git_raises_workflow_error(self):
        import assent.gitops as gitops
        original = gitops.subprocess.run

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("git not found")

        gitops.subprocess.run = fake_run
        try:
            with self.assertRaises(AssentError):
                ensure_clean(Path("."))
        finally:
            gitops.subprocess.run = original


if __name__ == "__main__":
    unittest.main()
