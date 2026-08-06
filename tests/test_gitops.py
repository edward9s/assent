"""gitops tests: all run inside a temporary repo created by tempfile.mkdtemp() (Rule 4)."""
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from unittest import mock

from assent import AssentError, gitops, lockfile, pathops, reconcile
from assent.gitops import (
    branches_with_prefix, changes_outside_scope, commit_all, commit_empty,
    commit_if_dirty,
    cleanup_unstarted_worktree, ensure_branch,
    ensure_clean, ensure_worktree, head_ref, resolve_folder_source, restore, tracked_paths,
    worktree_path)
from tests.link_support import cleanup_worktree, make_directory_link, safe_rmtree


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
        safe_rmtree(self.root)


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
                if pathops.is_link(path):
                    safe_rmtree(path)
                elif (path / ".git").is_file():
                    cleanup_worktree(self.root, path)
                else:
                    safe_rmtree(path)
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

    def test_explicit_start_snapshot_is_used_only_for_creation(self):
        original = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        (self.root / "base.txt").write_text("stack base\n", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "stack base")
        stack_base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        _run(self.root, "reset", "--hard", original)

        path = ensure_worktree(self.root, "parallel01", stack_base)
        self.assertEqual(head_ref(path), stack_base)
        self.assertTrue((path / "base.txt").is_file())

        reused = ensure_worktree(self.root, "parallel01", original)
        self.assertEqual(reused, path)
        self.assertEqual(head_ref(reused), stack_base)

    def test_cleanup_removes_only_clean_unstarted_resources(self):
        snapshot = head_ref(self.root)
        path = ensure_worktree(self.root, "parallel01", snapshot)
        branch = ensure_branch(path, "parallel01/")

        cleanup_unstarted_worktree(
            self.root, "parallel01", snapshot, "parallel01/")

        self.assertFalse(path.exists())
        self.assertNotIn(branch, branches_with_prefix(self.root, "parallel01/"))

    def test_cleanup_detaches_links_and_leaves_their_targets_untouched(self):
        # Setup-failure cleanup runs `git worktree remove`, which walks an
        # ignored junction into its target and deletes what it finds there, so
        # the link objects have to be gone before Git starts.
        (self.root / ".gitignore").write_text("pkg/\n", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "ignore pkg")
        snapshot = head_ref(self.root)
        path = ensure_worktree(self.root, "parallel01", snapshot)
        branch = ensure_branch(path, "parallel01/")
        target = Path(tempfile.mkdtemp())
        self.addCleanup(safe_rmtree, target)
        (target / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        make_directory_link(path / "pkg", target)

        cleanup_unstarted_worktree(
            self.root, "parallel01", snapshot, "parallel01/")

        self.assertFalse(path.exists())
        self.assertNotIn(branch, branches_with_prefix(self.root, "parallel01/"))
        self.assertEqual([entry.name for entry in target.iterdir()],
                         ["sentinel.txt"])
        self.assertEqual((target / "sentinel.txt").read_text(encoding="utf-8"),
                         "keep\n")

    def test_cleanup_refuses_dirty_new_worktree_and_preserves_it(self):
        snapshot = head_ref(self.root)
        path = ensure_worktree(self.root, "parallel01", snapshot)
        branch = ensure_branch(path, "parallel01/")
        (path / "keep.txt").write_text("keep\n", encoding="utf-8")

        with self.assertRaisesRegex(AssentError, "dirty.*retained"):
            cleanup_unstarted_worktree(
                self.root, "parallel01", snapshot, "parallel01/")

        self.assertTrue(path.exists())
        self.assertIn(branch, branches_with_prefix(self.root, "parallel01/"))

    def test_prunes_stale_metadata_and_recreates_deleted_worktree(self):
        path = ensure_worktree(self.root, "parallel01")
        safe_rmtree(path)

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


class TestNonTraversingInventory(GitTestCase):
    """The shared boundary refuses by name rather than widening what it deletes."""

    def tearDown(self) -> None:
        container = self.root.parent / f"{self.root.name}.worktrees"
        if container.exists():
            for path in container.iterdir():
                cleanup_worktree(self.root, path)
            container.rmdir()
        _run(self.root, "worktree", "prune")
        super().tearDown()

    def test_a_missing_path_has_nothing_to_detach(self):
        # Idempotence: a rerun after an interrupted cleanup must not fail here.
        self.assertEqual(
            pathops.inventory_directory_links(self.root / "already gone"), ())

    def test_a_linked_root_is_refused_instead_of_resolved(self):
        target = Path(tempfile.mkdtemp())
        self.addCleanup(safe_rmtree, target)
        (target / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        link = self.root / "linked root"
        make_directory_link(link, target)

        with self.assertRaisesRegex(AssentError, "root is itself a link"):
            pathops.inventory_directory_links(link)
        self.assertTrue((target / "sentinel.txt").is_file())

    def test_a_file_root_is_refused_by_name(self):
        with self.assertRaisesRegex(AssentError, "is not a directory"):
            pathops.inventory_directory_links(self.root / "README.md")

    def test_an_unreadable_entry_is_refused_by_name(self):
        (self.root / "unreadable").mkdir()
        real_lstat = os.lstat

        def failing_lstat(path, *args, **kwargs):
            if Path(path).name == "unreadable":
                raise PermissionError("simulated unreadable entry")
            return real_lstat(path, *args, **kwargs)

        with mock.patch("os.lstat", failing_lstat):
            with self.assertRaisesRegex(AssentError, "unable to inspect .*unreadable"):
                pathops.inventory_directory_links(self.root)

    def test_a_refused_detachment_stops_setup_failure_cleanup(self):
        (self.root / ".gitignore").write_text("pkg/\n", encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", "ignore pkg")
        snapshot = head_ref(self.root)
        path = ensure_worktree(self.root, "parallel01", snapshot)
        ensure_branch(path, "parallel01/")
        target = Path(tempfile.mkdtemp())
        self.addCleanup(safe_rmtree, target)
        (target / "sentinel.txt").write_text("keep\n", encoding="utf-8")
        make_directory_link(path / "pkg", target)

        with mock.patch.object(pathops, "detach_directory_link",
                               side_effect=OSError("simulated detachment failure")):
            with self.assertRaisesRegex(AssentError, "recoverable path retained"):
                cleanup_unstarted_worktree(
                    self.root, "parallel01", snapshot, "parallel01/")

        self.assertTrue(path.is_dir())
        self.assertTrue((path / "pkg").exists())
        self.assertTrue((target / "sentinel.txt").is_file())


class TestResolveFolderSource(GitTestCase):
    def tearDown(self) -> None:
        container = self.root.parent / f"{self.root.name}.worktrees"
        if container.exists():
            for path in container.iterdir():
                if pathops.is_link(path):
                    safe_rmtree(path)
                elif (path / ".git").is_file():
                    cleanup_worktree(self.root, path)
                else:
                    safe_rmtree(path)
            container.rmdir()
        _run(self.root, "worktree", "prune")
        super().tearDown()

    def make_source(self, folder: str = "upstream") -> Path:
        path = worktree_path(self.root, folder)
        _run(self.root, "worktree", "add", "-b", f"{folder}/run", str(path), "HEAD")
        (path / f"{folder}.txt").write_text("source\n", encoding="utf-8")
        _run(path, "add", "-A")
        _run(path, "commit", "-m", f"finish {folder}")
        return path

    def test_returns_exact_clean_attached_source_identity(self):
        path = self.make_source()

        source = resolve_folder_source(self.root, "upstream")

        self.assertEqual(source.folder, "upstream")
        self.assertEqual(source.branch, "upstream/run")
        self.assertEqual(source.worktree, path.resolve())
        self.assertEqual(source.tip, subprocess.run(
            ["git", "rev-parse", "upstream/run"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip())

    def test_missing_fixed_worktree_is_refused(self):
        _run(self.root, "branch", "upstream/run", "HEAD")
        with self.assertRaisesRegex(AssentError, "no valid fixed source worktree"):
            resolve_folder_source(self.root, "upstream")

    def test_ambiguous_branches_are_refused(self):
        self.make_source()
        _run(self.root, "branch", "upstream/other", "HEAD")
        with self.assertRaisesRegex(AssentError, "ambiguous source branches"):
            resolve_folder_source(self.root, "upstream")

    def test_dirty_source_is_refused(self):
        path = self.make_source()
        (path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "source worktree .* is dirty"):
            resolve_folder_source(self.root, "upstream")

    def test_detached_source_is_refused(self):
        path = self.make_source()
        _run(path, "checkout", "--detach")
        with self.assertRaisesRegex(AssentError, "source worktree .* is detached"):
            resolve_folder_source(self.root, "upstream")

    def test_foreign_source_branch_is_refused(self):
        path = self.make_source()
        _run(path, "checkout", "-b", "foreign")
        with self.assertRaisesRegex(AssentError, "foreign branch foreign"):
            resolve_folder_source(self.root, "upstream")


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


class TestCommitEmpty(GitTestCase):
    def test_empty_commit_records_terminal_evidence_without_staging_content(self):
        before = head_ref(self.root)
        commit_empty(self.root, "auto(plan01/t001): resumed task")
        after = head_ref(self.root)

        self.assertNotEqual(after, before)
        self.assertEqual(
            subprocess.run(["git", "diff-tree", "--no-commit-id", "--name-only",
                            "-r", after], cwd=self.root, capture_output=True,
                           encoding="utf-8", check=True).stdout.strip(), "")
        self.assertEqual(
            subprocess.run(["git", "log", "-1", "--pretty=%s"], cwd=self.root,
                           capture_output=True, encoding="utf-8", check=True).stdout.strip(),
            "auto(plan01/t001): resumed task")


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
            safe_rmtree(empty)


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


class TemporaryBranchTestCase(GitTestCase):
    """A repo whose temporary branches, worktrees and refs are easy to build and read."""

    def setUp(self) -> None:
        super().setUp()
        self.target = subprocess.run(
            ["git", "branch", "--show-current"], cwd=self.root,
            capture_output=True, encoding="utf-8", check=True).stdout.strip()
        self.worktrees: list[Path] = []

    def tearDown(self) -> None:
        for path in self.worktrees:
            cleanup_worktree(self.root, path)
        _run(self.root, "worktree", "prune")
        super().tearDown()

    def _commit_file(self, name: str, content: str,
                     message: str | None = None) -> None:
        (self.root / name).write_text(content, encoding="utf-8")
        _run(self.root, "add", "-A")
        _run(self.root, "commit", "-m", message or f"write {name}")

    def _branch_with_commit(self, branch: str, name: str, content: str) -> None:
        _run(self.root, "checkout", "-b", branch)
        self._commit_file(name, content)
        _run(self.root, "checkout", self.target)

    def _add_worktree(self, branch: str) -> Path:
        path = self.root.parent / f"{self.root.name}.checkout"
        _run(self.root, "worktree", "add", str(path), branch)
        self.worktrees.append(path)
        return path

    def _git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              encoding="utf-8", check=True).stdout

class TestTemporaryBranches(TemporaryBranchTestCase):
    """Read-only inventory of the two branch namespaces Assent owns."""

    def test_prefixes_have_exactly_one_definition(self):
        self.assertEqual(gitops.INTEGRATION_BRANCH_PREFIX, "assent-integration/")
        self.assertEqual(gitops.RECONCILE_BRANCH_PREFIX, "assent-reconcile/")
        self.assertIs(reconcile.RECONCILE_BRANCH_PREFIX,
                      gitops.RECONCILE_BRANCH_PREFIX)

    def test_repository_without_temporary_branches_is_empty(self):
        _run(self.root, "branch", "feature01")
        self.assertEqual(gitops.temporary_branches(self.root), ())

    def test_same_tree_off_the_target_is_published(self):
        # Exactly the accept shape: the target publishes a *different* commit
        # carrying the same tree, so ancestry would find nothing.
        self._branch_with_commit("assent-integration/folder01/aaaa",
                                 "shared.txt", "published\n")
        # accept(folder01): a distinct commit publishing the identical tree.
        self._commit_file("shared.txt", "published\n", "accept(folder01)")
        record, = gitops.temporary_branches(self.root)
        self.assertEqual(record.branch, "assent-integration/folder01/aaaa")
        self.assertEqual(record.classification, "published")
        self.assertTrue(record.is_published)
        self.assertNotEqual(record.tip, self._git("rev-parse", "HEAD").strip())
        self.assertEqual(record.tree, self._git("rev-parse", "HEAD^{tree}").strip())

    def test_tree_no_reachable_commit_carries_is_superseded(self):
        self._branch_with_commit("assent-reconcile/folder02", "only.txt", "gone\n")
        record, = gitops.temporary_branches(self.root)
        self.assertEqual(record.branch, "assent-reconcile/folder02")
        self.assertEqual(record.classification, "superseded")
        self.assertFalse(record.is_published)

    def test_records_are_ordered_by_branch_name(self):
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        self._branch_with_commit("assent-integration/folder03/bbbb", "c.txt", "c\n")
        self._branch_with_commit("assent-integration/folder01/aaaa", "a.txt", "a\n")
        self.assertEqual(
            [record.branch for record in gitops.temporary_branches(self.root)],
            ["assent-integration/folder01/aaaa",
             "assent-integration/folder03/bbbb",
             "assent-reconcile/folder02"])

    def test_checked_out_branch_is_reported_with_its_worktree(self):
        self._branch_with_commit("assent-integration/folder01/aaaa", "a.txt", "a\n")
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        path = self._add_worktree("assent-integration/folder01/aaaa")
        checked, free = gitops.temporary_branches(self.root)
        self.assertTrue(checked.is_checked_out)
        self.assertEqual(checked.checked_out_in.resolve(), path.resolve())
        self.assertIsNone(free.checked_out_in)
        self.assertFalse(free.is_checked_out)

    def test_call_mutates_nothing(self):
        self._branch_with_commit("assent-integration/folder01/aaaa", "a.txt", "a\n")
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        self._add_worktree("assent-reconcile/folder02")
        before_branches = self._git("branch", "--format=%(refname) %(objectname)")
        before_worktrees = self._git("worktree", "list", "--porcelain")
        gitops.temporary_branches(self.root)
        self.assertEqual(self._git("branch", "--format=%(refname) %(objectname)"),
                         before_branches)
        self.assertEqual(self._git("worktree", "list", "--porcelain"),
                         before_worktrees)

    def test_explicit_target_classifies_against_that_ref(self):
        self._branch_with_commit("assent-integration/folder01/aaaa",
                                 "shared.txt", "published\n")
        # accept(folder01): a distinct commit publishing the identical tree.
        self._commit_file("shared.txt", "published\n", "accept(folder01)")
        self.assertEqual(
            gitops.temporary_branches(self.root, "HEAD~1")[0].classification,
            "superseded")


class TestRemoveTemporaryBranches(TemporaryBranchTestCase):
    """Deleting the orphans the inventory found, under a lock the caller holds."""

    def _refs(self) -> list[str]:
        return sorted(self._git("show-ref").splitlines())

    def test_deletes_every_temporary_branch_and_leaves_other_refs_alone(self):
        self._branch_with_commit("assent-integration/folder01/aaaa", "a.txt", "a\n")
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        _run(self.root, "branch", "folder01/run", "HEAD")
        _run(self.root, "tag", "release01")
        temporary = gitops.temporary_branches(self.root)
        survivors = [ref for ref in self._refs()
                     if not any(f" refs/heads/{record.branch}" in ref
                                for record in temporary)]
        removals = gitops.remove_temporary_branches(self.root, temporary)
        self.assertEqual([(removal.branch, removal.outcome) for removal in removals],
                         [("assent-integration/folder01/aaaa", "deleted"),
                          ("assent-reconcile/folder02", "deleted")])
        self.assertEqual([removal.classification for removal in removals],
                         ["superseded", "superseded"])
        self.assertEqual(gitops.temporary_branches(self.root), ())
        self.assertEqual(self._refs(), survivors)

    def test_checked_out_branch_is_refused_and_the_rest_still_go(self):
        self._branch_with_commit("assent-integration/folder01/aaaa", "a.txt", "a\n")
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        path = self._add_worktree("assent-integration/folder01/aaaa")
        refused, deleted = gitops.remove_temporary_branches(
            self.root, gitops.temporary_branches(self.root))
        self.assertEqual(refused.outcome, "refused")
        self.assertEqual(refused.checked_out_in.resolve(), path.resolve())
        self.assertIsNone(refused.error)
        self.assertEqual(deleted.outcome, "deleted")
        self.assertEqual([record.branch for record in gitops.temporary_branches(self.root)],
                         ["assent-integration/folder01/aaaa"])

    def test_git_refusal_is_reported_and_the_other_branch_is_still_attempted(self):
        # Real Git refusal, no mock: the branch is genuinely checked out, but the
        # record claims otherwise, so only Git itself can stop the deletion.
        self._branch_with_commit("assent-integration/folder01/aaaa", "a.txt", "a\n")
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        self._add_worktree("assent-integration/folder01/aaaa")
        checked, free = gitops.temporary_branches(self.root)
        failed, deleted = gitops.remove_temporary_branches(
            self.root, (replace(checked, checked_out_in=None), free))
        self.assertEqual(failed.outcome, "failed")
        self.assertIn("assent-integration/folder01/aaaa", failed.error)
        self.assertIsNone(failed.checked_out_in)
        self.assertEqual(deleted.outcome, "deleted")
        self.assertEqual([record.branch for record in gitops.temporary_branches(self.root)],
                         ["assent-integration/folder01/aaaa"])

    def test_branch_outside_the_owned_prefixes_raises_instead_of_deleting(self):
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        _run(self.root, "branch", "folder01/run", "HEAD")
        record, = gitops.temporary_branches(self.root)
        with self.assertRaises(AssentError) as raised:
            gitops.remove_temporary_branches(
                self.root, (replace(record, branch="folder01/run"),))
        self.assertIn("folder01/run", str(raised.exception))
        self.assertIn(" refs/heads/folder01/run", "\n".join(self._refs()))

    def test_does_not_acquire_the_integration_lock(self):
        self._branch_with_commit("assent-reconcile/folder02", "b.txt", "b\n")
        with lockfile.hold_integration_lock(self.root / ".assent"):
            removal, = gitops.remove_temporary_branches(
                self.root, gitops.temporary_branches(self.root))
        self.assertEqual(removal.outcome, "deleted")
        self.assertEqual(gitops.temporary_branches(self.root), ())

    def test_removing_nothing_is_a_success(self):
        self.assertEqual(gitops.remove_temporary_branches(self.root, ()), ())


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
