"""reject 子命令測試：驗證駁回流程與 clean 模組完全分流。"""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agents.clean as clean
from agents import AgentsError, gitops
from agents.config import load_config
from agents.lockfile import hold_lock
from agents.plan import read_entries
from agents.reject import reject_folder


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class TestReject(unittest.TestCase):
    """人工裁決駁回的預檢、封存、強制清除與任務狀態重置。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".agents/\n", encoding="utf-8")
        (self.root / "README.md").write_text("起點\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "init")

        self.folder = "plan01"
        self.tasks_dir = self.root / ".agents" / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.root / ".agents" / "agents.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.tasks_dir / "agents.lock").write_text(
            'folder = "plan01"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.folder)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        shutil.rmtree(self.container, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _write_task(self, number: int, status: str) -> Path:
        path = self.tasks_dir / f"t{number:03d}_task.e.toml"
        path.write_text(
            'title = "任務"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["agents/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "完成任務"\n'
            'acceptance = "驗證通過"\n',
            encoding="utf-8")
        return path

    def _task_status(self, path: Path) -> str:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("status"):
                return line.split('"')[1]
        raise AssertionError(f"{path} 沒有 status 行")

    def _run_reject(self) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = reject_folder(self.cfg)
        return code, output.getvalue()

    def _worktree_branch(self, commit: bool = False) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, self.folder)
        branch = gitops.ensure_branch(worktree, f"{self.folder}/")
        if commit:
            (worktree / "成果.txt").write_text(branch, encoding="utf-8")
            gitops.commit_all(worktree, "完成成果")
        return worktree, branch

    def test_clean_module_does_not_expose_reject(self) -> None:
        self.assertFalse(hasattr(clean, "reject_folder"))

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
            self.root, f"{self.folder}/"), [])
        # 非同前綴分支(主分支)不受影響。
        self.assertTrue(_git(self.root, "branch", "--show-current"))
        self.assertIn(tip, output)
        self.assertIn(f"分支 {branch}(tip {tip}):已刪", output)
        # DONE -> TODO 並在 r 檔留下可救援的完整 Git 存證；SKIP/TODO 不動。
        self.assertEqual(self._task_status(done), "TODO")
        self.assertEqual(self._task_status(skipped), "SKIP")
        self.assertEqual(self._task_status(todo), "TODO")
        entries = read_entries(self.tasks_dir / "t001_task.r.toml")
        self.assertEqual(entries[-1]["event"], "rejected")
        self.assertEqual(entries[-1]["by"], "scheduler")
        self.assertIn(f"分支 {branch} tip {tip}", entries[-1]["detail"])
        self.assertFalse((self.tasks_dir / "t002_task.r.toml").exists())
        self.assertIn("駁回完成(重置 1 個任務為 TODO)", output)

    def test_reject_archives_dirty_worktree_before_removal(self) -> None:
        self._write_task(1, "WIP")
        worktree, branch = self._worktree_branch(commit=True)
        (worktree / "未提交.txt").write_text("殘留\n", encoding="utf-8")

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertFalse(worktree.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.folder}/"), [])
        self.assertIn("未提交變更已封存為 wip commit", output)

    def test_reject_invalid_plan_preserves_git_state(self) -> None:
        done = self._write_task(1, "DONE")
        with open(done, "a", encoding="utf-8") as stream:
            stream.write('unknown = "破壞格式"\n')
        worktree, branch = self._worktree_branch(commit=True)

        code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("任務檔無法解析", output)
        self.assertIn("Git 現場未變動", output)

    def test_reject_busy_lock_returns_one_and_touches_nothing(self) -> None:
        done = self._write_task(1, "DONE")
        worktree, branch = self._worktree_branch(commit=True)

        with hold_lock(self.tasks_dir, self.folder):
            code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("駁回中止(run 進行中)", output)

    def test_reject_missing_lock_returns_one(self) -> None:
        done = self._write_task(1, "DONE")
        (self.tasks_dir / "agents.lock").unlink()

        code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertEqual(self._task_status(done), "DONE")
        self.assertIn("駁回中止", output)

    def test_reject_git_failure_skips_task_reset(self) -> None:
        done = self._write_task(1, "DONE")
        self._worktree_branch(commit=True)

        with patch("agents.reject.gitops.delete_branch_force",
                   side_effect=AgentsError("模擬刪除失敗")):
            code, output = self._run_reject()

        self.assertEqual(code, 1)
        self.assertEqual(self._task_status(done), "DONE")
        self.assertFalse((self.tasks_dir / "t001_task.r.toml").exists())
        self.assertIn("任務檔未重置", output)

    def test_reject_is_idempotent_after_success(self) -> None:
        self._write_task(1, "DONE")
        self._worktree_branch(commit=True)
        first_code, _ = self._run_reject()
        self.assertEqual(first_code, 0)

        code, output = self._run_reject()

        self.assertEqual(code, 0)
        self.assertIn("worktree 不存在", output)
        self.assertIn("重置 0 個任務", output)


if __name__ == "__main__":
    unittest.main()
