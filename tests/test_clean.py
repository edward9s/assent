"""Tests for the clean subcommand: verify every safety proof and action order against
a real Git repo."""
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent import AssentError, gitops, pathops
from assent import clean as clean_mod
from assent.clean import clean_folder, clean_folders
from assent.config import load_config
from assent.lockfile import hold_integration_lock, hold_lock
from tests.link_support import make_directory_link, safe_rmtree


_VERIFY = 'python -c "raise SystemExit(0)"'


def _task_text(status: str = "DONE") -> str:
    return "\n".join((
        'title = "Cleanup task"',
        "deps = []",
        'model = "lite"',
        f"status = {json.dumps(status)}",
        'scope = ["src/"]',
        f"verify = {json.dumps(_VERIFY)}",
        'goal = "Keep cleanup safe."',
        'acceptance = "Cleanup preserves required evidence."',
        "",
    ))


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class TestClean(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(safe_rmtree, self.root)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "README.md").write_text("init\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "init")

        self.folder = "plan01"
        self.tasks_dir = self.root / ".assent" / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.root / ".assent" / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.tasks_dir / "t001_task.e.toml").write_text(
            _task_text(), encoding="utf-8")
        (self.tasks_dir / "assent.lock").write_text(
            'folder = "plan01"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.folder)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        safe_rmtree(self.container)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _assent_snapshot(self) -> list[tuple[str, bool, bytes]]:
        assent_dir = self.root / ".assent"
        return [
            (str(path.relative_to(assent_dir)), path.is_dir(),
             b"" if path.is_dir() else path.read_bytes())
            for path in sorted(assent_dir.rglob("*"))
        ]

    def _run_clean(self) -> tuple[int, str]:
        before = self._assent_snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = clean_folder(self.cfg)
        self.assertEqual(self._assent_snapshot(), before)
        return code, output.getvalue()

    def _worktree_branch(self, commit: bool = False) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, self.folder)
        branch = gitops.ensure_branch(worktree, f"{self.folder}/")
        if commit:
            (worktree / "result.txt").write_text(branch, encoding="utf-8")
            gitops.commit_all(worktree, "finish result")
        return worktree, branch

    def _folder(self, name: str, status: str = "DONE", *,
                after: list[str] | None = None, lock: bool = True) -> Path:
        folder = self.root / ".assent" / name
        folder.mkdir(exist_ok=True)
        (folder / "t001_task.e.toml").write_text(
            _task_text(status), encoding="utf-8")
        if after is not None:
            quoted = ", ".join(json.dumps(item) for item in after)
            (folder / "_folder.toml").write_text(
                f"after = [{quoted}]\n", encoding="utf-8")
        if lock:
            (folder / "assent.lock").write_text(
                f'folder = "{name}"\n', encoding="utf-8")
        return folder

    def _source(self, folder: str, *, commit: bool = True) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, folder)
        branch = gitops.ensure_branch(worktree, f"{folder}/")
        if commit:
            (worktree / f"{folder}.txt").write_text(
                f"result for {folder}\n", encoding="utf-8")
            gitops.commit_all(worktree, f"finish {folder}")
        return worktree, branch

    def _linked_merged_source(self) -> tuple[Path, str, list[tuple[str, str]]]:
        (self.root / ".gitignore").write_text(
            ".assent/\npkg/\nassets/\nlib/l10n/arb/\n", encoding="utf-8")
        (self.root / "lib" / "l10n").mkdir(parents=True)
        (self.root / "lib" / "l10n" / "app_en.arb").write_text(
            "{}\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore", "lib/l10n/app_en.arb")
        _git(self.root, "commit", "-m", "provision ignored cleanup targets")

        targets = {
            "pkg": {"sentinel.txt": "pkg sentinel\n"},
            "assets": {"sentinel.txt": "assets sentinel\n"},
            "lib/l10n/arb": {"app.arb": '{"keep": true}\n'},
        }
        for directory, contents in targets.items():
            for relative, content in contents.items():
                target = self.root / directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        before = self._target_inventory(targets)

        worktree, branch = self._worktree_branch(commit=True)
        make_directory_link(worktree / "pkg", self.root / "pkg")
        make_directory_link(worktree / "assets", self.root / "assets")
        make_directory_link(
            worktree / "lib" / "l10n" / "arb", self.root / "lib" / "l10n" / "arb")
        _git(self.root, "merge", "--no-ff", branch, "-m", "accept linked source")
        return worktree, branch, before

    def _target_inventory(self, targets: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
        return sorted(
            (str(path.relative_to(self.root)).replace("\\", "/"),
             hashlib.sha256(path.read_bytes()).hexdigest())
            for directory in targets
            for path in (self.root / directory).rglob("*")
            if path.is_file())

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

    def test_clean_detaches_main_tree_links_without_changing_targets(self) -> None:
        worktree, branch, before = self._linked_merged_source()

        code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)

    def test_clean_detachment_failure_is_retryable_and_fail_closed(self) -> None:
        worktree, branch, before = self._linked_merged_source()

        with patch.object(pathops, "detach_directory_link",
                          side_effect=OSError("simulated detachment failure")):
            code, output = self._run_clean()

        self.assertEqual(code, 1, output)
        self.assertIn("linked target content was not touched", output)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)

        code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertFalse(worktree.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.folder}/"), [])
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)

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

    def test_busy_integration_lock_refuses_cleanup(self) -> None:
        worktree, branch = self._worktree_branch()
        with hold_integration_lock(self.root / ".assent"):
            code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.folder_branches(self.root, self.folder))
        self.assertIn("repository integration is in progress", output)

    def test_lock_order_and_locked_graph_recheck(self) -> None:
        worktree, branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", branch)
        events: list[str] = []
        real_integration = clean_mod.hold_integration_lock
        real_probe = clean_mod.probe_lock

        @contextlib.contextmanager
        def traced_integration(path):
            events.append("enter integration")
            with real_integration(path):
                yield
            events.append("exit integration")

        @contextlib.contextmanager
        def traced_probe(path, folder):
            events.append(f"enter folder {folder}")
            with real_probe(path, folder):
                yield
            events.append(f"exit folder {folder}")

        with patch.object(clean_mod, "hold_integration_lock", traced_integration), \
                patch.object(clean_mod, "probe_lock", traced_probe), \
                patch.object(
                    clean_mod, "parse_folder_dependency_graph",
                    wraps=clean_mod.parse_folder_dependency_graph) as parse_graph:
            code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertFalse(worktree.exists())
        self.assertEqual(events, [
            "enter integration", f"enter folder {self.folder}",
            f"exit folder {self.folder}", "exit integration"])
        self.assertGreaterEqual(parse_graph.call_count, 2)

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
        second_dir = self.root / ".assent" / second
        second_dir.mkdir()
        (second_dir / "t001_task.e.toml").write_text(
            _task_text(), encoding="utf-8")
        (second_dir / "assent.lock").write_text(
            'folder = "plan02"\n', encoding="utf-8")
        second_branch = f"{second}/leftover"
        _git(self.root, "branch", second_branch, "HEAD")
        second_cfg = load_config(self.config_path, second)
        before = self._assent_snapshot()

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
        self.assertEqual(self._assent_snapshot(), before)

    def test_missing_lock_fails_closed_without_creating_one(self) -> None:
        (self.tasks_dir / "assent.lock").unlink()
        branch = f"{self.folder}/leftover"
        _git(self.root, "branch", branch, "HEAD")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertFalse((self.tasks_dir / "assent.lock").exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertIn("without modifying .assent", output)

    def test_clean_detached_unmerged_head_is_retained(self) -> None:
        worktree = gitops.ensure_worktree(self.root, self.folder)
        (worktree / "detached_result.txt").write_text("do not discard\n", encoding="utf-8")
        gitops.commit_all(worktree, "detached result")

        code, output = self._run_clean()

        self.assertEqual(code, 0)
        self.assertTrue(worktree.exists())
        self.assertIn("worktree HEAD not yet merged, retained", output)

    def test_unfinished_direct_dependents_retain_upstream_source(self) -> None:
        worktree, branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", branch)
        dependent = self._folder("dependent", after=[self.folder])

        for status in ("TODO", "WIP", "BLOCKED"):
            with self.subTest(status=status):
                (dependent / "t001_task.e.toml").write_text(
                    _task_text(status), encoding="utf-8")
                code, output = self._run_clean()
                self.assertEqual(code, 0, output)
                self.assertTrue(worktree.exists())
                self.assertIn(branch, gitops.folder_branches(
                    self.root, self.folder))
                self.assertIn(f"dependent dependent: unfinished tasks: t001={status}",
                              output)
                self.assertIn("upstream-first order", output)

    def test_completed_but_unaccepted_dependent_retains_upstream(self) -> None:
        upstream, upstream_branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", upstream_branch)
        self._folder("dependent", after=[self.folder])
        dependent, _branch = self._source("dependent")

        code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertTrue(upstream.exists())
        self.assertTrue(dependent.exists())
        self.assertIn("current source tip", output)
        self.assertIn("not integrated into the current target", output)

    def test_accepted_dependent_allows_upstream_first_cleanup(self) -> None:
        upstream, upstream_branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", upstream_branch)
        self._folder("dependent", after=[self.folder])
        dependent, dependent_branch = self._source("dependent")
        _git(self.root, "merge", "--no-ff", "-m", "accept dependent",
             dependent_branch)

        code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertFalse(upstream.exists())
        self.assertTrue(dependent.exists())
        self.assertIn(f"branch {upstream_branch}: cleaned", output)

    def test_completed_dependent_source_states_fail_closed(self) -> None:
        upstream, upstream_branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", upstream_branch)
        dependent_dir = self._folder("dependent", after=[self.folder])

        code, output = self._run_clean()
        self.assertEqual(code, 0, output)
        self.assertIn("no source worktree", output)
        self.assertTrue(upstream.exists())

        dependent, dependent_branch = self._source("dependent")
        (dependent / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        code, output = self._run_clean()
        self.assertEqual(code, 0, output)
        self.assertIn("source worktree", output)
        self.assertIn("not clean", output)
        (dependent / "dirty.txt").unlink()

        _git(dependent, "checkout", "--detach")
        code, output = self._run_clean()
        self.assertEqual(code, 0, output)
        self.assertIn("detached HEAD", output)

        _git(dependent, "checkout", dependent_branch)
        gitops.remove_worktree(self.root, dependent)
        _git(self.root, "branch", "dependent/second", "HEAD")
        code, output = self._run_clean()
        self.assertEqual(code, 0, output)
        self.assertIn("current source is ambiguous", output)
        self.assertTrue(dependent_dir.exists())
        self.assertTrue(upstream.exists())

    def test_only_direct_dependents_control_each_cleanup_boundary(self) -> None:
        upstream, upstream_branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", upstream_branch)
        self._folder("middle", after=[self.folder])
        middle, middle_branch = self._source("middle")
        _git(self.root, "merge", "--no-ff", "-m", "accept middle", middle_branch)
        self._folder("downstream", status="TODO", after=["middle"])

        code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertFalse(upstream.exists())
        self.assertTrue(middle.exists())
        middle_cfg = load_config(self.config_path, "middle")
        middle_output = io.StringIO()
        with contextlib.redirect_stdout(middle_output):
            middle_code = clean_folder(middle_cfg)
        self.assertEqual(middle_code, 0, middle_output.getvalue())
        self.assertTrue(middle.exists())
        self.assertIn("dependent downstream: unfinished tasks: t001=TODO",
                      middle_output.getvalue())

    def test_bad_unrelated_plan_and_cycle_fail_before_deletion(self) -> None:
        worktree, branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", branch)
        unrelated = self._folder("unrelated")
        (unrelated / "t001_task.e.toml").write_text(
            "not valid = [\n", encoding="utf-8")

        code, output = self._run_clean()

        self.assertEqual(code, 1, output)
        self.assertTrue(worktree.exists())
        self.assertIn("dependency evidence could not be parsed", output)

        (unrelated / "t001_task.e.toml").write_text(
            _task_text(), encoding="utf-8")
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["unrelated"]\n', encoding="utf-8")
        (unrelated / "_folder.toml").write_text(
            f'after = ["{self.folder}"]\n', encoding="utf-8")
        code, output = self._run_clean()
        self.assertEqual(code, 1, output)
        self.assertTrue(worktree.exists())
        self.assertIn("form a cycle", output)

    def test_dependent_lock_is_held_through_destructive_cleanup(self) -> None:
        upstream, upstream_branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", upstream_branch)
        dependent_dir = self._folder("dependent", after=[self.folder])
        _dependent, dependent_branch = self._source("dependent")
        _git(self.root, "merge", "--no-ff", "-m", "accept dependent",
             dependent_branch)
        real_remove = gitops.remove_worktree
        lock_was_busy = False

        def observe_lock(root: Path, path: Path) -> None:
            nonlocal lock_was_busy
            try:
                with hold_lock(dependent_dir, "dependent"):
                    pass
            except AssentError:
                lock_was_busy = True
            real_remove(root, path)

        with patch.object(gitops, "remove_worktree", side_effect=observe_lock):
            code, output = self._run_clean()

        self.assertEqual(code, 0, output)
        self.assertTrue(lock_was_busy)
        self.assertFalse(upstream.exists())

    def test_clean_all_uses_upstream_first_order(self) -> None:
        upstream, upstream_branch = self._worktree_branch(commit=True)
        _git(self.root, "merge", "--ff-only", upstream_branch)
        self._folder("dependent", after=[self.folder])
        dependent, dependent_branch = self._source("dependent")
        _git(self.root, "merge", "--no-ff", "-m", "accept dependent",
             dependent_branch)
        dependent_cfg = load_config(self.config_path, "dependent")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = clean_folders([dependent_cfg, self.cfg])

        self.assertEqual(code, 0, output.getvalue())
        self.assertFalse(upstream.exists())
        self.assertFalse(dependent.exists())
        self.assertLess(output.getvalue().index(f"{self.folder}: cleaned"),
                        output.getvalue().index("dependent: cleaned"))


if __name__ == "__main__":
    unittest.main()
