"""End-to-end tests for explicit, transactional local folder acceptance."""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent import accept as accept_mod
from assent import gitops
from assent.accept import accept_folder
from assent.config import load_config
from assent.gitops import AcceptStatus
from assent.lockfile import hold_integration_lock, hold_lock

_DEFAULT_VERIFY = "python .assent/verify.py"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class AcceptRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent accept test "))
        self.root = self.parent / "repository with spaces"
        self.root.mkdir()
        self.addCleanup(self._cleanup)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Assent Test")
        _git(self.root, "config", "user.email", "assent@example.invalid")
        _git(self.root, "checkout", "-b", "trunk")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")

        self.folder = "plan01"
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.assent_dir / "verify.py").write_text(
            "import sys\nsys.exit(0)\n", encoding="utf-8")

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

    def _write_task(self, task_id: str = "t001", status: str = "DONE", *,
                    folder: str | None = None,
                    verify: str = _DEFAULT_VERIFY) -> Path:
        tasks_dir = self.assent_dir / (folder or self.folder)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{task_id}_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "prime"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            f'verify = {verify!r}\n'
            'goal = "Complete the task."\n'
            'acceptance = "Verification passes."\n',
            encoding="utf-8")
        return path

    def _make_source(self, folder: str | None = None, *,
                     filename: str | None = None, content: str = "result\n"
                     ) -> tuple[Path, str, str]:
        folder = folder or self.folder
        filename = filename or f"{folder}-result.txt"
        worktree = gitops.ensure_worktree(self.root, folder)
        branch = gitops.ensure_branch(worktree, f"{folder}/")
        (worktree / filename).write_text(content, encoding="utf-8")
        gitops.commit_all(worktree, f"finish {folder}")
        return worktree, branch, gitops.branch_tip(self.root, branch)

    def _accept(self, folder: str | None = None) -> tuple[int, str]:
        output = io.StringIO()
        cfg = load_config(self.config_path, folder or self.folder)
        with contextlib.redirect_stdout(output):
            code = accept_folder(cfg)
        return code, output.getvalue()

    def _head(self, ref: str = "HEAD") -> str:
        return _git(self.root, "rev-parse", ref)

    def _assert_no_temporary_state(self) -> None:
        container = self.parent / f"{self.root.name}.integration"
        self.assertFalse(container.exists() and list(container.iterdir()))
        self.assertEqual(
            gitops.branches_with_prefix(self.root, "assent-integration/"), [])


class TestAcceptSuccess(AcceptRepositoryCase):
    def test_creates_one_evidenced_merge_and_keeps_source(self) -> None:
        self._write_task("t001", "DONE")
        self._write_task("t002", "SKIP")
        worktree, branch, source_tip = self._make_source()
        before = self._head()

        code, output = self._accept()

        self.assertEqual(code, 0, output)
        after = self._head()
        parents = _git(
            self.root, "rev-list", "--parents", "-n", "1", after).split()
        self.assertEqual(parents[1:], [before, source_tip])
        evidence = gitops.find_accept_evidence(self.root, self.folder, "trunk")
        self.assertEqual(evidence.status, AcceptStatus.ACCEPTED)
        self.assertEqual(evidence.evidence.source_branch, branch)
        self.assertEqual(evidence.evidence.source_tip, source_tip)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.folder_branches(self.root, self.folder))
        for value in (self.folder, branch, source_tip, "trunk", before, after,
                      "verification passed", "evidence merge commit"):
            self.assertIn(value, output)
        self._assert_no_temporary_state()

        code, output = self._accept()
        self.assertEqual(code, 0, output)
        self.assertEqual(self._head(), after)
        self.assertIn("already accepted", output)

    def test_cleaned_source_is_recognized_by_evidence(self) -> None:
        self._write_task()
        worktree, branch, _ = self._make_source()
        self.assertEqual(self._accept()[0], 0)
        gitops.remove_worktree(self.root, worktree)
        gitops.delete_branch_force(self.root, branch)
        accepted = self._head()

        code, output = self._accept()

        self.assertEqual(code, 0, output)
        self.assertEqual(self._head(), accepted)
        self.assertIn("already accepted and cleaned", output)

    def test_unique_branch_without_worktree_uses_snapshot(self) -> None:
        self._write_task()
        worktree, branch, source_tip = self._make_source()
        gitops.remove_worktree(self.root, worktree)

        code, output = self._accept()

        self.assertEqual(code, 0, output)
        self.assertEqual(
            _git(self.root, "rev-parse", "HEAD^2"), source_tip)
        self.assertIn(branch, gitops.folder_branches(self.root, self.folder))
        self._assert_no_temporary_state()

    def test_verify_commands_are_filename_ordered_and_deduplicated(self) -> None:
        self._write_task("t002", "DONE", verify="second")
        self._write_task("t001", "DONE", verify="first")
        self._write_task("t003", "DONE", verify="first")
        self._write_task("t004", "SKIP", verify="skipped")
        plan = accept_mod.Plan.parse(self.tasks_dir)
        self.assertEqual(accept_mod._verify_commands(plan), ["first", "second"])


class TestAcceptPrechecks(AcceptRepositoryCase):
    def test_unfinished_statuses_all_refuse_before_git_change(self) -> None:
        for status in ("TODO", "WIP", "BLOCKED"):
            with self.subTest(status=status):
                shutil.rmtree(self.tasks_dir, ignore_errors=True)
                self._write_task(status=status)
                before = self._head()
                code, output = self._accept()
                self.assertEqual(code, 1, output)
                self.assertIn("not finished", output)
                self.assertEqual(self._head(), before)

    def test_busy_folder_and_integration_locks_refuse_and_release(self) -> None:
        self._write_task()
        self._make_source()
        before = self._head()
        cfg = load_config(self.config_path, self.folder)

        with hold_lock(self.tasks_dir, self.folder):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("already processing", output)
        with hold_integration_lock(self.assent_dir):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("integration is already running", output)
        self.assertEqual(self._head(), before)

        with patch("assent.accept._run_verify", return_value=1):
            self.assertEqual(accept_folder(cfg), 1)
        code, output = self._accept()
        self.assertEqual(code, 0, output)

    def test_target_must_be_clean_and_attached(self) -> None:
        self._write_task()
        self._make_source()
        before = self._head()
        (self.root / "dirty.txt").write_text("dirty", encoding="utf-8")
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("main worktree", output)
        self.assertEqual(self._head(), before)

        (self.root / "dirty.txt").unlink()
        _git(self.root, "checkout", "--detach", "HEAD")
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("detached HEAD", output)
        self.assertEqual(self._head(), before)

    def test_source_must_be_clean_attached_and_on_folder_branch(self) -> None:
        self._write_task()
        cases = ("dirty", "detached", "foreign")
        for case in cases:
            with self.subTest(case=case):
                if case != "dirty":
                    gitops.remove_worktree(self.root, worktree)
                    worktree = gitops.ensure_worktree(self.root, self.folder)
                if case == "dirty":
                    worktree, _, _ = self._make_source()
                    (worktree / "dirty.txt").write_text("dirty", encoding="utf-8")
                    expected = "source worktree"
                elif case == "detached":
                    expected = "detached HEAD"
                else:
                    _git(worktree, "checkout", "-b", "foreign/run")
                    expected = "not a plan01/* branch"
                before = self._head()
                code, output = self._accept()
                self.assertEqual(code, 1, output)
                self.assertIn(expected, output)
                self.assertEqual(self._head(), before)
                if case == "dirty":
                    (worktree / "dirty.txt").unlink()

    def test_ambiguous_or_absent_source_refuses_without_evidence(self) -> None:
        self._write_task()
        before = self._head()
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("no trustworthy accept evidence", output)

        _git(self.root, "branch", f"{self.folder}/one", "HEAD")
        _git(self.root, "branch", f"{self.folder}/two", "HEAD")
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("multiple candidate", output)
        self.assertEqual(self._head(), before)

    def test_bad_target_plan_or_folder_graph_fails_closed(self) -> None:
        before = self._head()
        malformed = self.tasks_dir / "t001_task.e.toml"
        malformed.write_text("not valid = [\n", encoding="utf-8")
        code, _ = self._accept()
        self.assertEqual(code, 1)
        self.assertEqual(self._head(), before)

        malformed.unlink()
        self._write_task()
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("does not exist", output)
        self.assertEqual(self._head(), before)

    def test_prerequisite_accepts_either_ancestry_or_evidence(self) -> None:
        self._write_task(folder="base")
        _, base_branch, _ = self._make_source("base")
        _git(self.root, "merge", "--no-ff", "-m", "manual base merge", base_branch)

        self._write_task(folder="dependent")
        dependent_dir = self.assent_dir / "dependent"
        (dependent_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        self._make_source("dependent")
        code, output = self._accept("dependent")
        self.assertEqual(code, 0, output)

        self._write_task(folder="cleaned")
        self._make_source("cleaned")
        self.assertEqual(self._accept("cleaned")[0], 0)
        cleaned_worktree = gitops.folder_worktree(self.root, "cleaned")
        gitops.remove_worktree(self.root, cleaned_worktree)
        for branch in gitops.folder_branches(self.root, "cleaned"):
            gitops.delete_branch_force(self.root, branch)

        self._write_task(folder="afterclean")
        after_dir = self.assent_dir / "afterclean"
        (after_dir / "_folder.toml").write_text(
            'after = ["cleaned"]\n', encoding="utf-8")
        self._make_source("afterclean")
        code, output = self._accept("afterclean")
        self.assertEqual(code, 0, output)

    def test_unaccepted_prerequisite_is_named_and_refused(self) -> None:
        self._write_task(folder="base")
        self._make_source("base")
        self._write_task(folder="dependent")
        (self.assent_dir / "dependent" / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        self._make_source("dependent")
        before = self._head()

        code, output = self._accept("dependent")

        self.assertEqual(code, 1, output)
        self.assertIn("base", output)
        self.assertIn("not yet accepted", output)
        self.assertEqual(self._head(), before)


class TestAcceptTransactionalFailures(AcceptRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        self._write_task()
        self.worktree, self.branch, self.source_tip = self._make_source()

    def _assert_preserved(self, before: str) -> None:
        self.assertEqual(self._head("trunk"), before)
        self.assertTrue(self.worktree.exists())
        self.assertIn(self.branch, gitops.folder_branches(self.root, self.folder))
        self._assert_no_temporary_state()

    def test_source_and_post_merge_verification_failures_preserve_state(self) -> None:
        before = self._head()
        with patch("assent.accept._run_verify", return_value=1):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("source verification failed", output)
        self._assert_preserved(before)

        calls = {"count": 0}

        def fail_second(*args):
            calls["count"] += 1
            return 1 if calls["count"] == 2 else 0

        with patch("assent.accept._run_verify", side_effect=fail_second):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("post-merge verification failed", output)
        self._assert_preserved(before)

    def test_merge_conflict_lists_paths_and_preserves_state(self) -> None:
        (self.worktree / "README.md").write_text("source\n", encoding="utf-8")
        gitops.commit_all(self.worktree, "source conflict")
        (self.root / "README.md").write_text("target\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "target conflict")
        before = self._head()

        code, output = self._accept()

        self.assertEqual(code, 1, output)
        self.assertIn("Conflicting file(s)", output)
        self.assertIn("README.md", output)
        self._assert_preserved(before)

    def test_target_move_and_branch_switch_never_advance_wrong_ref(self) -> None:
        before = self._head()
        real_run_verifies = accept_mod._run_verifies
        calls = {"count": 0}

        def move_target(cfg, tree, commands):
            calls["count"] += 1
            if calls["count"] == 2:
                (self.root / "concurrent.txt").write_text("move", encoding="utf-8")
                _git(self.root, "add", "concurrent.txt")
                _git(self.root, "commit", "-m", "concurrent target move")
            return real_run_verifies(cfg, tree, commands)

        with patch.object(accept_mod, "_run_verifies", move_target):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("moved during accept", output)
        moved = self._head()
        self.assertNotEqual(moved, before)
        self._assert_no_temporary_state()

        _git(self.root, "reset", "--hard", before)
        _git(self.root, "branch", "other", before)
        calls["count"] = 0

        def switch_target(cfg, tree, commands):
            calls["count"] += 1
            if calls["count"] == 2:
                _git(self.root, "switch", "other")
            return real_run_verifies(cfg, tree, commands)

        with patch.object(accept_mod, "_run_verifies", switch_target):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("no longer on trunk", output)
        self.assertEqual(self._head("trunk"), before)
        self.assertEqual(self._head("other"), before)
        self._assert_no_temporary_state()


if __name__ == "__main__":
    unittest.main()
