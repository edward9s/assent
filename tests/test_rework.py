"""Tests for non-destructive reopen of a single task and its downstream-dependency cascade."""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest

from tests.engine_support import models_block
from pathlib import Path
from unittest.mock import patch

from assent import AssentError, gitops
from assent.config import load_config
from assent.lockfile import hold_lock
from assent.plan import read_entries, set_status
from assent.rework import rework_task, rework_tasks_locked


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class TestRework(unittest.TestCase):
    """Verify with a real temporary repo that reopening destroys no Git output."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "README.md").write_text("起點\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "init")

        self.plan_name = "plan01"
        self.tasks_dir = self.root / ".assent" / self.plan_name
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.root / ".assent" / "assent.toml"
        self.config_path.write_text(
            '[workflow]\ntask = [{ action = "focused_test" }]\n'
            + models_block(), encoding="utf-8")
        (self.tasks_dir / "assent.lock").write_text(
            'plan = "plan01"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.plan_name)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        shutil.rmtree(self.container, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _write_task(self, number: int, status: str,
                    deps: tuple[int, ...] = ()) -> Path:
        dependencies = ", ".join(f'"t{item:03d}"' for item in deps)
        path = self.tasks_dir / f"t{number:03d}_task.e.toml"
        path.write_text(
            'title = "任務"\n'
            f'deps = [{dependencies}]\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'verify = "python -m unittest"\n'
            'goal = "完成任務"\n'
            'acceptance = "驗證通過"\n',
            encoding="utf-8")
        return path

    @staticmethod
    def _status(path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("status"):
                return line.split('"')[1]
        raise AssertionError(f"{path} has no status line")

    def _run(self, task_id: str = "t001", **kwargs) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rework_task(self.cfg, task_id, **kwargs)
        return code, output.getvalue()

    def _worktree(self, prefix: str | None = None) -> tuple[Path, str]:
        path = gitops.ensure_worktree(self.root, self.plan_name)
        branch = gitops.ensure_branch(path, prefix or self.cfg.branch_prefix)
        return path, branch

    def test_clean_reject_and_rework_modules_remain_separate(self) -> None:
        import assent.clean as clean_module
        import assent.reject as reject_module
        import assent.rework as rework_module

        self.assertFalse(hasattr(clean_module, "reject_plan"))
        self.assertFalse(hasattr(clean_module, "rework_task"))
        self.assertFalse(hasattr(reject_module, "rework_task"))
        self.assertFalse(hasattr(rework_module, "clean_plans"))
        self.assertFalse(hasattr(rework_module, "reject_plan"))

    def test_rework_transaction_is_split_into_named_phases(self) -> None:
        import inspect

        import assent.rework as rework_module

        coordinator = inspect.getsource(rework_module._rework_locked)
        for phase in ("_resolve_request", "_resume_interrupted_revert",
                      "_prepare_management_plane", "_prepare_git_scene",
                      "_apply_code_revert", "_build_log_values",
                      "_persist_status_first", "_persist_journal_first"):
            with self.subTest(phase=phase):
                self.assertTrue(callable(getattr(rework_module, phase)))
                self.assertIn(phase, coordinator)
        # The coordinator only sequences the phases; it must not absorb their bodies again.
        self.assertLess(len(coordinator.splitlines()), 40)

    def test_locked_automatic_rework_reopens_only_finding_owners(self):
        first = self._write_task(1, "DONE")
        second = self._write_task(2, "DONE", deps=(1,))
        worktree, _branch = self._worktree()
        cfg = self.cfg.for_worktree(worktree)
        with hold_lock(self.tasks_dir, self.plan_name):
            code = rework_tasks_locked(
                cfg, ["t001"], "review finding needs repair")
        self.assertEqual(code, 0)
        self.assertEqual(self._status(first), "TODO")
        self.assertEqual(self._status(second), "DONE")
        entry = read_entries(first.with_name("t001_task.r.toml"))[-1]
        self.assertIn("Automatic repair rework", entry["summary"])
        self.assertIn("authorization: configured workflow repair",
                      entry["detail"])
        self.assertIn("cascade scope: disabled", entry["detail"])

    def test_locked_automatic_rework_keeps_exact_dependent_owners(self):
        first = self._write_task(1, "DONE")
        second = self._write_task(2, "DONE", deps=(1,))
        third = self._write_task(3, "DONE", deps=(2,))
        worktree, _branch = self._worktree()
        cfg = self.cfg.for_worktree(worktree)

        with hold_lock(self.tasks_dir, self.plan_name):
            code = rework_tasks_locked(
                cfg, ["t001", "t002"], "two findings need repair")

        self.assertEqual(code, 0)
        self.assertEqual(self._status(first), "TODO")
        self.assertEqual(self._status(second), "TODO")
        self.assertEqual(self._status(third), "DONE")

    def test_all_non_todo_target_statuses_can_reopen(self) -> None:
        task = self._write_task(1, "DONE")
        head = _git(self.root, "rev-parse", "HEAD")
        for status in ("DONE", "WIP", "BLOCKED", "SKIP"):
            with self.subTest(status=status):
                set_status(task, status)
                code, _ = self._run(reason="")
                self.assertEqual(code, 0)
                self.assertEqual(self._status(task), "TODO")
                entry = read_entries(
                    self.tasks_dir / "t001_task.r.toml")[-1]
                self.assertEqual(entry["by"], "scheduler")
                self.assertEqual(entry["event"], "rework_requested")
                self.assertIn(f"original status: {status}", entry["detail"])
                self.assertIn(f"HEAD: {head}", entry["detail"])
                self.assertIn("reason: manual rework requested", entry["detail"])

    def test_exact_id_and_todo_are_rejected_without_mutation(self) -> None:
        task = self._write_task(1, "TODO")

        missing_code, missing_output = self._run("T001")
        todo_code, todo_output = self._run()

        self.assertEqual(missing_code, 1)
        self.assertEqual(todo_code, 1)
        self.assertEqual(self._status(task), "TODO")
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())
        self.assertIn("exact task id not found: T001", missing_output)
        self.assertIn("no rework needed", todo_output)

    def test_success_refreshes_report_with_target_and_cascade_statuses(self) -> None:
        target = self._write_task(1, "DONE")
        downstream = self._write_task(2, "BLOCKED", (1,))

        code, output = self._run(cascade=True)

        self.assertEqual(code, 0, output)
        report = (self.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("t001", report)
        self.assertIn("t002", report)
        self.assertEqual(self._status(target), "TODO")
        self.assertEqual(self._status(downstream), "TODO")
        self.assertGreaterEqual(report.count("TODO"), 2)

    def test_report_write_failure_returns_one_after_rework(self) -> None:
        task = self._write_task(1, "DONE")
        report = self.tasks_dir / "_report.md"
        report.write_text("舊報告\n", encoding="utf-8")

        with patch("assent.rework.write_report",
                   side_effect=PermissionError("報告檔被鎖定")):
            code, output = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(self._status(task), "TODO")
        self.assertEqual(report.read_text(encoding="utf-8"), "舊報告\n")
        self.assertIn("task reopened, but report update failed", output)

    def test_failed_rework_does_not_generate_new_report(self) -> None:
        self._write_task(1, "TODO")

        with patch("assent.rework.write_report") as mocked:
            code, _ = self._run()

        self.assertEqual(code, 1)
        mocked.assert_not_called()

    def test_downstream_blockers_are_complete_and_in_plan_order(self) -> None:
        target = self._write_task(1, "DONE")
        second = self._write_task(2, "DONE", (1,))
        third = self._write_task(3, "BLOCKED", (2,))
        fourth = self._write_task(4, "WIP", (1,))
        self._write_task(5, "TODO", (1,))
        self._write_task(6, "SKIP", (2,))

        code, output = self._run()

        self.assertEqual(code, 1)
        self.assertIn("t002, t003, t004", output)
        self.assertEqual(self._status(target), "DONE")
        self.assertEqual(self._status(second), "DONE")
        self.assertEqual(self._status(third), "BLOCKED")
        self.assertEqual(self._status(fourth), "WIP")
        self.assertEqual(list(self.tasks_dir.glob("*.r.toml")), [])

    def test_cascade_reopens_active_downstream_but_preserves_todo_and_skip(self) -> None:
        target = self._write_task(1, "SKIP")
        done = self._write_task(2, "DONE", (1,))
        blocked = self._write_task(3, "BLOCKED", (2,))
        wip = self._write_task(4, "WIP", (1,))
        todo = self._write_task(5, "TODO", (1,))
        skipped = self._write_task(6, "SKIP", (2,))

        code, _ = self._run(cascade=True, reason="規格需要重做")

        self.assertEqual(code, 0)
        for path in (target, done, blocked, wip, todo):
            self.assertEqual(self._status(path), "TODO")
        self.assertEqual(self._status(skipped), "SKIP")
        for number, original in ((1, "SKIP"), (2, "DONE"),
                                 (3, "BLOCKED"), (4, "WIP")):
            entries = read_entries(
                self.tasks_dir / f"t{number:03d}_task.r.toml")
            self.assertEqual(len(entries), 1)
            self.assertIn(f"original status: {original}", entries[0]["detail"])
            self.assertIn("cascade scope: t002, t003, t004, t005, t006",
                          entries[0]["detail"])
            self.assertIn("reason: 規格需要重做", entries[0]["detail"])
        self.assertFalse((self.tasks_dir / "t005_task.r.toml").exists())
        self.assertFalse((self.tasks_dir / "t006_task.r.toml").exists())

    def test_dirty_worktree_is_checkpointed_and_fully_retained(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, branch = self._worktree()
        (worktree / "既有成果.txt").write_text("已提交\n", encoding="utf-8")
        gitops.commit_all(worktree, "既有成果")
        prior = _git(worktree, "rev-parse", "HEAD")
        (worktree / "未提交成果.txt").write_text("保留\n", encoding="utf-8")

        code, _ = self._run()

        current = _git(worktree, "rev-parse", "HEAD")
        self.assertEqual(code, 0)
        self.assertNotEqual(current, prior)
        self.assertTrue((worktree / "未提交成果.txt").is_file())
        self.assertEqual(_git(worktree, "branch", "--show-current"), branch)
        self.assertIn(prior, _git(worktree, "rev-list", "HEAD"))
        self.assertIn("manual rework pre-archive",
                      _git(worktree, "log", "-1", "--pretty=%s"))
        entry = read_entries(task.with_name("t001_task.r.toml"))[-1]
        self.assertIn(f"HEAD: {current}", entry["detail"])

    def test_missing_worktree_only_changes_management_state(self) -> None:
        task = self._write_task(1, "BLOCKED")
        main_head = _git(self.root, "rev-parse", "HEAD")

        code, output = self._run()

        self.assertEqual(code, 0)
        self.assertEqual(self._status(task), "TODO")
        self.assertFalse(self.container.exists())
        self.assertEqual(_git(self.root, "rev-parse", "HEAD"), main_head)
        self.assertIn("only reopening management state", output)

    def test_fake_worktree_and_wrong_branch_fail_closed(self) -> None:
        task = self._write_task(1, "DONE")
        fake = gitops.worktree_path(self.root, self.plan_name)
        fake.mkdir(parents=True)
        (fake / "不可動.txt").write_text("保留\n", encoding="utf-8")

        fake_code, fake_output = self._run()

        self.assertEqual(fake_code, 1)
        self.assertEqual(self._status(task), "DONE")
        self.assertIn("is not a valid worktree of this repo", fake_output)
        shutil.rmtree(fake)
        worktree, _ = self._worktree("other/")

        branch_code, branch_output = self._run()

        self.assertEqual(branch_code, 1)
        self.assertEqual(self._status(task), "DONE")
        self.assertTrue(worktree.exists())
        self.assertIn("on a branch outside this plan", branch_output)
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())

    def test_checkpoint_failure_does_not_touch_status_or_journal(self) -> None:
        task = self._write_task(1, "WIP")
        worktree, _ = self._worktree()
        (worktree / "未提交成果.txt").write_text("保留\n", encoding="utf-8")

        with patch("assent.rework.gitops.commit_if_dirty",
                   side_effect=AssentError("模擬封存失敗")):
            code, output = self._run()

        self.assertEqual(code, 1)
        self.assertEqual(self._status(task), "WIP")
        self.assertTrue((worktree / "未提交成果.txt").is_file())
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())
        self.assertIn("模擬封存失敗", output)

    def test_bad_task_and_bad_journal_fail_before_git_checkpoint(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        dirty = worktree / "未提交成果.txt"
        dirty.write_text("保留\n", encoding="utf-8")
        before = _git(worktree, "rev-parse", "HEAD")
        journal = self.tasks_dir / "t001_task.r.toml"
        journal.write_text("不是 TOML = [\n", encoding="utf-8")

        bad_journal_code, bad_journal_output = self._run()

        self.assertEqual(bad_journal_code, 1)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(self._status(task), "DONE")
        self.assertIn("management-plane precheck failed", bad_journal_output)
        journal.unlink()
        with task.open("a", encoding="utf-8") as stream:
            stream.write('unknown = "錯誤"\n')

        bad_task_code, bad_task_output = self._run()

        self.assertEqual(bad_task_code, 1)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertTrue(dirty.is_file())
        self.assertIn("task files could not be parsed", bad_task_output)

    def test_busy_lock_blocks_but_missing_lock_is_created(self) -> None:
        task = self._write_task(1, "DONE")
        with hold_lock(self.tasks_dir, self.plan_name):
            busy_code, busy_output = self._run()
        self.assertEqual(busy_code, 1)
        self.assertEqual(self._status(task), "DONE")
        self.assertIn("a run is in progress", busy_output)
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())

        (self.tasks_dir / "assent.lock").unlink()
        missing_code, missing_output = self._run()

        self.assertEqual(missing_code, 0, missing_output)
        self.assertEqual(self._status(task), "TODO")
        self.assertTrue((self.tasks_dir / "assent.lock").is_file())
        self.assertIn("rework complete", missing_output)
        self.assertTrue((self.tasks_dir / "t001_task.r.toml").is_file())

    def test_partial_journal_write_can_be_retried_without_duplicates(self) -> None:
        target = self._write_task(1, "DONE")
        downstream = self._write_task(2, "BLOCKED", (1,))
        from assent.plan import append_entry as real_append_entry

        calls = 0

        def interrupt_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("模擬日誌中斷")
            return real_append_entry(*args, **kwargs)

        with patch("assent.rework.append_entry", side_effect=interrupt_second):
            first_code, _ = self._run(cascade=True)
        second_code, _ = self._run(cascade=True)

        self.assertEqual(first_code, 1)
        self.assertEqual(second_code, 0)
        self.assertEqual(self._status(target), "TODO")
        self.assertEqual(self._status(downstream), "TODO")
        self.assertEqual(len(read_entries(
            self.tasks_dir / "t001_task.r.toml")), 1)
        self.assertEqual(len(read_entries(
            self.tasks_dir / "t002_task.r.toml")), 1)

    def test_partial_status_write_resumes_before_target(self) -> None:
        target = self._write_task(1, "DONE")
        second = self._write_task(2, "DONE", (1,))
        third = self._write_task(3, "BLOCKED", (2,))
        real_set_status = set_status
        calls = 0

        def interrupt_second(path, status):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("模擬狀態中斷")
            return real_set_status(path, status)

        with patch("assent.rework.set_status", side_effect=interrupt_second):
            first_code, _ = self._run(cascade=True)
        self.assertEqual(self._status(target), "DONE")
        second_code, _ = self._run(cascade=True)

        self.assertEqual(first_code, 1)
        self.assertEqual(second_code, 0)
        for path in (target, second, third):
            self.assertEqual(self._status(path), "TODO")
        for number in (1, 2, 3):
            self.assertEqual(len(read_entries(
                self.tasks_dir / f"t{number:03d}_task.r.toml")), 1)

    def test_default_path_never_calls_destructive_git_helpers(self) -> None:
        self._write_task(1, "DONE")
        helper_names = ("restore", "remove_worktree", "delete_branch",
                        "delete_branch_force")
        patches = [patch(f"assent.rework.gitops.{name}")
                   for name in helper_names]
        mocks = [item.start() for item in patches]
        self.addCleanup(lambda: [item.stop() for item in patches])

        code, _ = self._run()

        self.assertEqual(code, 0)
        for mocked in mocks:
            mocked.assert_not_called()

    def test_revert_code_reverses_leaf_checkpoint_and_records_hashes(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        result = worktree / "成果.txt"
        result.write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        original = _git(worktree, "rev-parse", "HEAD")

        code, output = self._run(revert_code=True, reason="重新設計")

        checkpoint = _git(worktree, "rev-parse", "HEAD")
        self.assertEqual(code, 0)
        self.assertEqual(self._status(task), "TODO")
        self.assertFalse(result.exists())
        self.assertNotEqual(checkpoint, original)
        self.assertIn(original, _git(worktree, "rev-list", "HEAD"))
        self.assertEqual(
            _git(worktree, "log", "-1", "--pretty=%s"),
            "rework(plan01/t001): revert task output")
        entry = read_entries(task.with_name("t001_task.r.toml"))[-1]
        self.assertIn(f"HEAD before operation: {original}", entry["detail"])
        self.assertIn(f"revert checkpoint: {checkpoint}", entry["detail"])
        self.assertIn(f"reverted hashes: {original}", entry["detail"])
        self.assertIn("reverted cascade set: disabled", entry["detail"])
        self.assertIn(f"  - {original}", output)

    def test_revert_code_reverses_multiple_tasks_newest_first(self) -> None:
        target = self._write_task(1, "DONE")
        downstream = self._write_task(2, "BLOCKED", (1,))
        worktree, _ = self._worktree()
        first = worktree / "一.txt"
        second = worktree / "二.txt"
        first.write_text("第一版\n", encoding="utf-8")
        gitops.commit_all(worktree, "wip(plan01/t001): 第一段")
        oldest = _git(worktree, "rev-parse", "HEAD")
        second.write_text("下游\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t002): 下游成果")
        middle = _git(worktree, "rev-parse", "HEAD")
        first.write_text("第二版\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 最終成果")
        newest = _git(worktree, "rev-parse", "HEAD")

        code, _ = self._run(cascade=True, revert_code=True)

        self.assertEqual(code, 0)
        self.assertEqual(self._status(target), "TODO")
        self.assertEqual(self._status(downstream), "TODO")
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        entry = read_entries(target.with_name("t001_task.r.toml"))[-1]
        self.assertIn(
            f"reverted hashes: {newest}, {middle}, {oldest}",
            entry["detail"])
        self.assertIn("reverted cascade set: t001, t002", entry["detail"])

    def test_revert_code_rejects_dirty_missing_and_bad_branches(self) -> None:
        task = self._write_task(1, "DONE")

        missing_code, missing_output = self._run(revert_code=True)
        worktree = gitops.ensure_worktree(self.root, self.plan_name)
        detached_code, detached_output = self._run(revert_code=True)
        gitops.ensure_branch(worktree, "other/")
        wrong_code, wrong_output = self._run(revert_code=True)

        self.assertEqual((missing_code, detached_code, wrong_code), (1, 1, 1))
        self.assertEqual(self._status(task), "DONE")
        self.assertIn("worktree does not exist", missing_output)
        self.assertIn("detached HEAD", detached_output)
        self.assertIn("on a branch outside this plan", wrong_output)
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_revert_code_dirty_is_rejected_without_checkpoint(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        result = worktree / "成果.txt"
        result.write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        before = _git(worktree, "rev-parse", "HEAD")
        dirty = worktree / "未知變更.txt"
        dirty.write_text("不得封存\n", encoding="utf-8")

        code, output = self._run(revert_code=True)

        self.assertEqual(code, 1)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(self._status(task), "DONE")
        self.assertTrue(dirty.exists())
        self.assertIn("Working tree is not clean", output)
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_revert_code_requires_checkpoint_continuous_at_head(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()

        none_code, none_output = self._run(revert_code=True)
        result = worktree / "成果.txt"
        result.write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        manual = worktree / "人工.txt"
        manual.write_text("人工調整\n", encoding="utf-8")
        gitops.commit_all(worktree, "人工提交")
        before = _git(worktree, "rev-parse", "HEAD")

        gap_code, gap_output = self._run(revert_code=True)

        self.assertEqual((none_code, gap_code), (1, 1))
        self.assertIn("no code checkpoint available for automatic reversion",
                      none_output)
        self.assertIn("does not form a safely revertible continuous tail",
                      gap_output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(self._status(task), "DONE")
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_revert_code_rejects_other_task_and_legacy_checkpoint(self) -> None:
        task = self._write_task(1, "DONE")
        self._write_task(2, "TODO")
        worktree, _ = self._worktree()
        (worktree / "目標.txt").write_text("目標\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 目標成果")
        (worktree / "其他.txt").write_text("其他\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t002): 其他成果")
        other_head = _git(worktree, "rev-parse", "HEAD")

        other_code, other_output = self._run(revert_code=True)
        _git(worktree, "commit", "--amend", "-m", "auto(t002): 舊格式")
        legacy_head = _git(worktree, "rev-parse", "HEAD")
        legacy_code, legacy_output = self._run(revert_code=True)

        self.assertEqual((other_code, legacy_code), (1, 1))
        self.assertIn("does not form a safely revertible continuous tail",
                      other_output)
        self.assertIn("does not form a safely revertible continuous tail",
                      legacy_output)
        self.assertNotEqual(other_head, legacy_head)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), legacy_head)
        self.assertEqual(self._status(task), "DONE")
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_revert_code_requires_cascade_before_git_mutation(self) -> None:
        target = self._write_task(1, "DONE")
        downstream = self._write_task(2, "DONE", (1,))
        worktree, _ = self._worktree()
        (worktree / "目標.txt").write_text("目標\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 目標成果")
        (worktree / "下游.txt").write_text("下游\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t002): 下游成果")
        before = _git(worktree, "rev-parse", "HEAD")

        code, output = self._run(revert_code=True)

        self.assertEqual(code, 1)
        self.assertIn("specify cascade: t002", output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(self._status(target), "DONE")
        self.assertEqual(self._status(downstream), "DONE")
        self.assertEqual(list(self.tasks_dir.glob("*.r.toml")), [])

    def test_revert_checkpoint_is_a_boundary_for_later_requests(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        (worktree / "成果.txt").write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        first_code, _ = self._run(revert_code=True)
        set_status(task, "DONE")
        before = _git(worktree, "rev-parse", "HEAD")

        second_code, output = self._run(revert_code=True)

        self.assertEqual((first_code, second_code), (0, 1))
        self.assertIn("no code checkpoint available for automatic reversion",
                      output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(self._status(task), "DONE")

    def test_revert_code_rejects_merge_in_checkpoint_tail(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, branch = self._worktree()
        result = worktree / "成果.txt"
        result.write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        _git(worktree, "checkout", "-b", "side")
        (worktree / "旁支.txt").write_text("旁支\n", encoding="utf-8")
        gitops.commit_all(worktree, "旁支提交")
        _git(worktree, "checkout", branch)
        _git(worktree, "merge", "--no-ff", "side", "-m",
             "auto(plan01/t001): 不合法的 merge checkpoint")
        before = _git(worktree, "rev-parse", "HEAD")

        code, output = self._run(revert_code=True)

        self.assertEqual(code, 1)
        self.assertIn("merge commit", output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), before)
        self.assertEqual(self._status(task), "DONE")

    def _prepare_real_revert_conflict(self) -> tuple[Path, Path, str, str]:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        (self.root / "README.md").write_text("旁支版本\n", encoding="utf-8")
        gitops.commit_all(self.root, "旁支衝突來源")
        conflicting = _git(self.root, "rev-parse", "HEAD")
        (worktree / "README.md").write_text("任務版本\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 任務成果")
        original = _git(worktree, "rev-parse", "HEAD")
        return task, worktree, conflicting, original

    def test_revert_conflict_aborts_to_original_clean_state(self) -> None:
        task, worktree, conflicting, original = \
            self._prepare_real_revert_conflict()
        real_revert = gitops.revert_no_commit

        def conflict(path, commits):
            return real_revert(path, [conflicting])

        with patch("assent.rework.gitops.revert_no_commit",
                   side_effect=conflict):
            code, output = self._run(revert_code=True)

        self.assertEqual(code, 1)
        self.assertIn("aborted and restored", output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"), original)
        self.assertEqual(_git(worktree, "status", "--porcelain"), "")
        self.assertEqual(
            (worktree / "README.md").read_text(encoding="utf-8"),
            "任務版本\n")
        self.assertEqual(self._status(task), "DONE")
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_revert_abort_failure_is_loud_and_keeps_management_state(self) -> None:
        task, worktree, conflicting, _ = self._prepare_real_revert_conflict()
        real_revert = gitops.revert_no_commit

        def conflict(path, commits):
            return real_revert(path, [conflicting])

        with patch("assent.rework.gitops.revert_no_commit",
                   side_effect=conflict), patch(
                       "assent.rework.gitops.abort_revert",
                       side_effect=AssentError("模擬 abort 失敗")):
            code, output = self._run(revert_code=True)

        self.assertEqual(code, 1)
        self.assertIn("git revert --abort also failed", output)
        self.assertIn(f"manual intervention required: {worktree}", output)
        self.assertEqual(self._status(task), "DONE")
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_landed_commit_message_mismatch_is_retained_without_abort(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        (worktree / "result.txt").write_text("done\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): completed result")
        before = gitops.commit_of(worktree, "HEAD")
        real_commit_all = gitops.commit_all

        def landed_then_report_mismatch(path, message, excludes=()):
            real_commit_all(path, message, excludes)
            commit = gitops.commit_of(path, "HEAD")
            raise gitops.CommitPostconditionError(
                f"git commit created {commit}, but message was changed; "
                "the commit was retained",
                commit)

        with patch("assent.rework.gitops.commit_all",
                   side_effect=landed_then_report_mismatch), patch(
                       "assent.rework.gitops.abort_revert") as abort:
            code, output = self._run(revert_code=True)

        retained = gitops.commit_of(worktree, "HEAD")
        self.assertEqual(code, 1, output)
        self.assertNotEqual(retained, before)
        self.assertIn("no revert abort was attempted", output)
        self.assertIn(retained, output)
        abort.assert_not_called()
        self.assertEqual(self._status(task), "DONE")
        self.assertFalse(task.with_name("t001_task.r.toml").exists())

    def test_revert_journal_interruption_resumes_without_second_revert(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        (worktree / "成果.txt").write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")

        with patch("assent.rework.append_entry",
                   side_effect=OSError("模擬日誌中斷")):
            first_code, first_output = self._run(
                revert_code=True, reason="重新設計")
        revert_checkpoint = _git(worktree, "rev-parse", "HEAD")

        second_code, second_output = self._run(
            revert_code=True, reason="重新設計")

        self.assertEqual(first_code, 1, first_output)
        self.assertEqual(second_code, 0, second_output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"),
                         revert_checkpoint)
        self.assertEqual(self._status(task), "TODO")
        entries = read_entries(task.with_name("t001_task.r.toml"))
        self.assertEqual(len(entries), 1)
        self.assertIn(
            f"revert checkpoint: {revert_checkpoint}", entries[0]["detail"])
        report = (self.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("TODO", report)
        self.assertIn("resuming an incomplete revert checkpoint", second_output)

    def test_revert_journal_persisted_before_error_finishes_report(self) -> None:
        task = self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        (worktree / "成果.txt").write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        from assent.plan import append_entry as real_append_entry

        def persist_then_fail(*args, **kwargs):
            real_append_entry(*args, **kwargs)
            raise OSError("模擬寫入成功後驗證失敗")

        with patch("assent.rework.append_entry", side_effect=persist_then_fail):
            first_code, first_output = self._run(
                revert_code=True, reason="重新設計")
        revert_checkpoint = _git(worktree, "rev-parse", "HEAD")
        second_code, second_output = self._run(
            revert_code=True, reason="重新設計")

        self.assertEqual(first_code, 0, first_output)
        self.assertEqual(second_code, 0, second_output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"),
                         revert_checkpoint)
        self.assertEqual(self._status(task), "TODO")
        entries = read_entries(task.with_name("t001_task.r.toml"))
        self.assertEqual(len(entries), 1)
        self.assertIn(
            f"revert checkpoint: {revert_checkpoint}", entries[0]["detail"])
        report = (self.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn("TODO", report)
        self.assertIn("management data is fully persisted", first_output)
        self.assertIn("continuing to update the report", second_output)

    def test_revert_status_interruption_resumes_cascade_without_second_revert(
            self) -> None:
        target = self._write_task(1, "DONE")
        downstream = self._write_task(2, "BLOCKED", (1,))
        worktree, _ = self._worktree()
        (worktree / "目標.txt").write_text("目標\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 目標成果")
        (worktree / "下游.txt").write_text("下游\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t002): 下游成果")
        real_set_status = set_status
        calls = 0

        def interrupt_target(path, status):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("模擬狀態中斷")
            return real_set_status(path, status)

        with patch("assent.rework.set_status", side_effect=interrupt_target):
            first_code, first_output = self._run(
                cascade=True, revert_code=True, reason="一起重做")
        revert_checkpoint = _git(worktree, "rev-parse", "HEAD")
        second_code, second_output = self._run(
            cascade=True, revert_code=True, reason="一起重做")

        self.assertEqual(first_code, 1, first_output)
        self.assertEqual(second_code, 0, second_output)
        self.assertEqual(_git(worktree, "rev-parse", "HEAD"),
                         revert_checkpoint)
        self.assertEqual(self._status(target), "TODO")
        self.assertEqual(self._status(downstream), "TODO")
        for task in (target, downstream):
            entries = read_entries(task.with_name(
                task.name.replace(".e.toml", ".r.toml")))
            self.assertEqual(len(entries), 1)
            self.assertIn(
                f"revert checkpoint: {revert_checkpoint}",
                entries[0]["detail"])
        report = (self.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertGreaterEqual(report.count("TODO"), 2)
        self.assertIn("resuming an incomplete revert checkpoint", second_output)

    def test_revert_code_never_runs_forbidden_git_commands(self) -> None:
        self._write_task(1, "DONE")
        worktree, _ = self._worktree()
        (worktree / "成果.txt").write_text("完成\n", encoding="utf-8")
        gitops.commit_all(worktree, "auto(plan01/t001): 完成成果")
        real_run = gitops._run_git
        calls: list[tuple[str, ...]] = []

        def recording(root, *args):
            calls.append(args)
            return real_run(root, *args)

        with patch("assent.gitops._run_git", side_effect=recording):
            code, _ = self._run(revert_code=True)

        self.assertEqual(code, 0)
        joined = [" ".join(args) for args in calls]
        for forbidden in ("reset --hard", "restore", "clean", "branch -D",
                          "tag -d", "tag --delete"):
            self.assertFalse(any(forbidden in command for command in joined),
                             (forbidden, joined))


if __name__ == "__main__":
    unittest.main()
