"""工作資料夾檔案鎖測試。

跨進程互斥用 subprocess 起一個持鎖子進程來驗證(比照 tests/test_e2e.py 以
subprocess 演練真實行為的慣例);同一進程內以假 flock 難以模擬跨 run 的
OS 層互斥,故走真子進程。
"""
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agents.config import load_config
from agents.lockfile import LOCK_NAME, LockBusy, hold_lock

# import agents 需要 repo 根目錄在路徑上;子進程以此為 cwd。
_REPO_ROOT = Path(__file__).resolve().parents[1]

# 子進程:取得鎖後印一行 LOCKED 通知父進程,再讀 stdin 阻塞持鎖;
# 父進程關閉其 stdin 即釋放鎖並退出。
_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from agents.lockfile import hold_lock
    with hold_lock(Path(sys.argv[1]), sys.argv[2]):
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        sys.stdin.readline()
    """
)


class TestHoldLock(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tasks_dir = self.root / ".agents" / "parallel01"
        self.tasks_dir.mkdir(parents=True)

    def _start_holder(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", _HOLDER, str(self.tasks_dir), "parallel01"],
            cwd=str(_REPO_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8")
        self.addCleanup(self._cleanup_holder, proc)
        line = proc.stdout.readline()  # 阻塞直到子進程確認持鎖
        self.assertEqual(line.strip(), "LOCKED")
        return proc

    def _cleanup_holder(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    def _release_holder(self, proc: subprocess.Popen) -> None:
        proc.stdin.close()  # 子進程 readline 收到 EOF -> 離開 with -> 釋放鎖
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_second_run_blocked_with_pid_and_folder(self):
        """子進程持鎖時,同資料夾第二次取鎖立即失敗,訊息含先行者 PID 與資料夾名。"""
        proc = self._start_holder()
        with self.assertRaises(LockBusy) as ctx:
            with hold_lock(self.tasks_dir, "parallel01"):
                pass
        msg = str(ctx.exception)
        self.assertIn("parallel01", msg)
        self.assertIn(str(proc.pid), msg)

    def test_reacquire_after_release(self):
        """持鎖進程結束後,無需任何清理即可立即再次取鎖。"""
        proc = self._start_holder()
        self._release_holder(proc)
        with hold_lock(self.tasks_dir, "parallel01"):
            pass  # 不拋即代表再次取鎖成功

    def test_reacquire_after_kill(self):
        """持鎖進程被 kill(非正常結束)後,OS 自動釋放鎖,可立即再取。"""
        proc = self._start_holder()
        proc.kill()
        proc.wait(timeout=10)
        with hold_lock(self.tasks_dir, "parallel01"):
            pass

    def test_lockfile_survives_release(self):
        """鎖檔是執行期產物,釋放後仍留在磁碟(刪檔會引入 race)。"""
        with hold_lock(self.tasks_dir, "parallel01"):
            pass
        self.assertTrue((self.tasks_dir / LOCK_NAME).is_file())

    def test_git_excludes_contains_lockfile(self):
        """Config.git_excludes 含鎖檔相對路徑(不進版控、不參與乾淨/scope 檢查)。"""
        (self.root / ".agents" / "agents.toml").write_text(
            '', encoding="utf-8")
        cfg = load_config(
            self.root / ".agents" / "agents.toml", "parallel01")
        self.assertEqual(cfg.lockfile_rel, ".agents/parallel01/agents.lock")
        self.assertIn(".agents/parallel01/agents.lock", cfg.git_excludes)


if __name__ == "__main__":
    unittest.main()
