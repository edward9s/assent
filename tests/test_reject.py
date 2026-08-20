"""Tests for the reject subcommand: verify the reject flow is fully split from the
clean module."""
import contextlib
import hashlib
import io
import shutil
import subprocess
import tempfile
import unittest

from tests.engine_support import models_block
from pathlib import Path
from unittest.mock import patch

import assent.clean as clean
from assent import AssentError, gitops, pathops
from assent.config import load_config
from assent.lockfile import hold_lock
from assent.plan import read_entries
from assent.reject import reject_plan
from tests.link_support import make_directory_link, safe_rmtree


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class TestReject(unittest.TestCase):
    """Reject's precheck, archival, force-removal, and task-status reset."""

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

        self.plan_name = "plan01"
        self.tasks_dir = self.root / ".assent" / self.plan_name
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.root / ".assent" / "assent.toml"
        self.config_path.write_text(models_block(), encoding="utf-8")
        (self.tasks_dir / "assent.lock").write_text(
            'plan = "plan01"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.plan_name)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        safe_rmtree(self.container)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _write_task(self, number: int, status: str) -> Path:
        path = self.tasks_dir / f"t{number:03d}_task.e.toml"
        path.write_text(
            'title = "task"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "finish task"\n'
            'acceptance = "verification passes"\n',
            encoding="utf-8")
        return path

    def _task_status(self, path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("status"):
                return line.split('"')[1]
        raise AssertionError(f"{path} has no status line")

    def _run_reject(self, confirm=lambda prompt: "y") -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = reject_plan(self.cfg, confirm=confirm)
        return code, output.getvalue()

    def _worktree_branch(self, commit: bool = False) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, self.plan_name)
        branch = gitops.ensure_branch(worktree, f"{self.plan_name}/")
        if commit:
            (worktree / "result.txt").write_text(branch, encoding="utf-8")
            gitops.commit_all(worktree, "finish result")
        return worktree, branch

    def _linked_worktree(self) -> tuple[Path, str, list[tuple[str, str]]]:
        (self.root / ".gitignore").write_text(
            ".assent/\npkg/\nassets/\nlib/l10n/arb/\n", encoding="utf-8")
        (self.root / "lib" / "l10n").mkdir(parents=True)
        (self.root / "lib" / "l10n" / "app_en.arb").write_text(
            "{}\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore", "lib/l10n/app_en.arb")
        _git(self.root, "commit", "-m", "provision ignored reject targets")

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
        return worktree, branch, before

    def _target_inventory(self, targets: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
        return sorted(
            (str(path.relative_to(self.root)).replace("\\", "/"),
             hashlib.sha256(path.read_bytes()).hexdigest())
            for directory in targets
            for path in (self.root / directory).rglob("*")
            if path.is_file())

    def _dependent_plan(self, name: str, status: str = "DONE", *,
                          after: list[str] | None = None, lock: bool = True) -> Path:
        plan_name = self.root / ".assent" / name
        plan_name.mkdir(exist_ok=True)
        (plan_name / "t001_task.e.toml").write_text(
            'title = "task"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "finish task"\n'
            'acceptance = "verification passes"\n',
            encoding="utf-8")
        if after is not None:
            quoted = ", ".join(f'"{item}"' for item in after)
            (plan_name / "_plan_deps.toml").write_text(
                f"after = [{quoted}]\n", encoding="utf-8")
        if lock:
            (plan_name / "assent.lock").write_text(
                f'plan = "{name}"\n', encoding="utf-8")
        return plan_name

    def _dependent_source(self, name: str, *, commit: bool = True) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, name)
        branch = gitops.ensure_branch(worktree, f"{name}/")
        if commit:
            (worktree / f"{name}.txt").write_text(
                f"result for {name}\n", encoding="utf-8")
            gitops.commit_all(worktree, f"finish {name}")
        return worktree, branch

    def test_clean_module_does_not_expose_reject(self) -> None:
        self.assertFalse(hasattr(clean, "reject_plan"))

    def test_reject_removes_unmerged_state_and_resets_tasks(self) -> None:
        done = self._write_task(1, "DONE")
        skipped = self._write_task(2, "SKIP")
        todo = self._write_task(3, "TODO")
        worktree, branch = self._worktree_branch(commit=True)
        tip = gitops.commit_of(self.root, branch)
        self.assertEqual(tip, _git(self.root, "rev-parse", branch))

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertFalse(self.container.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"), [])
        # A branch without this prefix (the main branch) is unaffected.
        self.assertTrue(_git(self.root, "branch", "--show-current"))
        self.assertIn(tip, output)
        self.assertIn(f"branch {branch} (tip {tip}): deleted", output)
        # DONE -> TODO leaves recoverable full Git evidence in the r file; SKIP/TODO
        # are untouched.
        self.assertEqual(self._task_status(done), "TODO")
        self.assertEqual(self._task_status(skipped), "SKIP")
        self.assertEqual(self._task_status(todo), "TODO")
        entries = read_entries(self.tasks_dir / "t001_task.r.toml")
        self.assertEqual(entries[-1]["event"], "rejected")
        self.assertEqual(entries[-1]["by"], "scheduler")
        self.assertIn(f"branch {branch} tip {tip}", entries[-1]["detail"])
        self.assertFalse((self.tasks_dir / "t002_task.r.toml").exists())
        self.assertIn("reject complete (1 task(s) reset to TODO)", output)

    def test_reject_archives_dirty_worktree_before_removal(self) -> None:
        self._write_task(1, "WIP")
        worktree, branch = self._worktree_branch(commit=True)
        (worktree / "uncommitted.txt").write_text("leftover\n", encoding="utf-8")

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"), [])
        self.assertIn("uncommitted changes archived as a wip commit", output)

    def test_reject_detaches_main_tree_links_before_resetting_tasks(self) -> None:
        done = self._write_task(1, "DONE")
        worktree, branch, before = self._linked_worktree()

        code, output = self._run_reject()

        self.assertEqual(code, 0, output)
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._task_status(done), "TODO")
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)

    def test_reject_detachment_failure_retains_git_and_task_state_for_retry(self) -> None:
        done = self._write_task(1, "DONE")
        worktree, branch, before = self._linked_worktree()

        with patch.object(pathops, "detach_directory_link",
                          side_effect=OSError("simulated detachment failure")):
            code, output = self._run_reject()

        self.assertEqual(code, 1, output)
        self.assertIn("linked target content was not touched", output)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)

        code, output = self._run_reject()

        self.assertEqual(code, 0, output)
        self.assertFalse(worktree.exists())
        self.assertEqual(self._task_status(done), "TODO")
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)

    def test_reject_invalid_plan_preserves_git_state(self) -> None:
        done = self._write_task(1, "DONE")
        with open(done, "a", encoding="utf-8") as stream:
            stream.write('unknown = "broken format"\n')
        worktree, branch = self._worktree_branch(commit=True)

        code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("task files could not be parsed", output)
        self.assertIn("Git scene unchanged", output)

    def test_reject_busy_lock_returns_one_and_touches_nothing(self) -> None:
        done = self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)

        with hold_lock(self.tasks_dir, self.plan_name):
            code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("reject aborted (a run is in progress)", output)

    def test_reject_missing_lock_returns_one(self) -> None:
        done = self._write_task(1, "DONE")
        (self.tasks_dir / "assent.lock").unlink()

        code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("reject aborted", output)

    def test_reject_git_failure_skips_task_reset(self) -> None:
        done = self._write_task(1, "DONE")
        self._worktree_branch(commit=True)

        with patch("assent.reject.gitops.delete_branch_force",
                   side_effect=AssentError("simulated deletion failure")):
            code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertEqual(self._task_status(done), "DONE")
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())
        self.assertIn("task files not reset", output)

    def test_reject_is_idempotent_after_success(self) -> None:
        self._write_task(1, "DONE")
        self._worktree_branch(commit=True)
        first_code, _ = self._run_reject()
        self.assertEqual(first_code, 0)

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertIn("worktree does not exist", output)
        self.assertIn("0 task(s) reset", output)

    def test_reject_declined_confirmation_touches_nothing(self) -> None:
        done = self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)

        code, output = self._run_reject(confirm=lambda prompt: "n")

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("cancelled", output)
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())

    def test_reject_eof_on_confirmation_touches_nothing(self) -> None:
        done = self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)

        def _raise_eof(prompt: str) -> str:
            raise EOFError()

        code, output = self._run_reject(confirm=_raise_eof)

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("cancelled", output)
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())

    def test_reject_without_dependents_output_unchanged(self) -> None:
        self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertNotIn("dependent", output)
        self.assertNotIn("stranded", output)
        self.assertNotIn("Two ways forward", output)

    def test_reject_unaccepted_dependent_defaults_to_refused(self) -> None:
        self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)
        self._dependent_plan("downstream", status="TODO", after=[self.plan_name])

        code, output = self._run_reject(confirm=lambda prompt: "n")

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertIn("downstream", output)
        self.assertIn("unfinished tasks: t001=TODO", output)
        self.assertIn("Two ways forward", output)
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())

    def test_reject_unaccepted_dependent_confirmed_records_stranded(self) -> None:
        self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)
        self._dependent_plan("downstream", status="TODO", after=[self.plan_name])

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertIn("downstream", output)
        entries = read_entries(self.tasks_dir / "t001_task.r.toml")
        self.assertEqual(entries[-1]["event"], "rejected")
        self.assertIn("downstream", entries[-1]["detail"])
        self.assertIn("stranding", entries[-1]["detail"].lower())

    def test_reject_dependent_lock_busy_refuses_without_prompt(self) -> None:
        self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)
        dependent_dir = self._dependent_plan(
            "downstream", status="TODO", after=[self.plan_name])

        def _fail_confirm(prompt: str) -> str:
            raise AssertionError("confirm must not be asked when a dependent is busy")

        with hold_lock(dependent_dir, "downstream"):
            code, output = self._run_reject(confirm=_fail_confirm)

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertIn("downstream", output)
        self.assertIn("its plan is being changed by another run", output)
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())

    def test_reject_accepted_dependent_does_not_trigger_guard(self) -> None:
        self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)
        self._dependent_plan("downstream", status="DONE", after=[self.plan_name])
        _dependent, dependent_branch = self._dependent_source("downstream")
        _git(self.root, "merge", "--no-ff", "-m", "accept downstream", dependent_branch)

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertNotIn("stranded", output)
        self.assertNotIn("Two ways forward", output)
        entries = read_entries(self.tasks_dir / "t001_task.r.toml")
        self.assertNotIn("stranding", entries[-1]["detail"].lower())


if __name__ == "__main__":
    unittest.main()
