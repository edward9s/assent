"""Task-folder file lock tests.

Cross-process mutual exclusion is verified by launching a lock-holding subprocess (following
the tests/test_e2e.py convention of exercising real behavior via subprocess); faking flock
within a single process cannot simulate the OS-level mutual exclusion across separate runs,
hence a real subprocess.
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

# Importing agents requires the repo root on the path; the subprocess uses it as cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Subprocess: after acquiring the lock, print a LOCKED line to notify the parent, then
# block on reading stdin while holding the lock; the parent closing its stdin releases
# the lock and lets it exit.
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
        line = proc.stdout.readline()  # blocks until the subprocess confirms it holds the lock
        self.assertEqual(line.strip(), "LOCKED")
        return proc

    def _cleanup_holder(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)

    def _release_holder(self, proc: subprocess.Popen) -> None:
        proc.stdin.close()  # subprocess readline gets EOF -> exits the with block -> releases the lock
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_second_run_blocked_with_pid_and_folder(self):
        """While the subprocess holds the lock, a second acquire on the same folder fails immediately, with a message including the holder's PID and folder name."""
        proc = self._start_holder()
        with self.assertRaises(LockBusy) as ctx:
            with hold_lock(self.tasks_dir, "parallel01"):
                pass
        msg = str(ctx.exception)
        self.assertIn("parallel01", msg)
        self.assertIn(str(proc.pid), msg)

    def test_reacquire_after_release(self):
        """After the lock-holding process exits, the lock can be reacquired immediately with no cleanup."""
        proc = self._start_holder()
        self._release_holder(proc)
        with hold_lock(self.tasks_dir, "parallel01"):
            pass  # not raising means reacquiring succeeded

    def test_reacquire_after_kill(self):
        """After the lock-holding process is killed (abnormal exit), the OS releases the lock automatically and it can be reacquired immediately."""
        proc = self._start_holder()
        proc.kill()
        proc.wait(timeout=10)
        with hold_lock(self.tasks_dir, "parallel01"):
            pass

    def test_lockfile_survives_release(self):
        """The lock file is a runtime artifact and stays on disk after release (deleting it would introduce a race)."""
        with hold_lock(self.tasks_dir, "parallel01"):
            pass
        self.assertTrue((self.tasks_dir / LOCK_NAME).is_file())

    def test_git_excludes_contains_lockfile(self):
        """Config.git_excludes contains the lock file's relative path (not tracked by git, not part of clean/scope checks)."""
        (self.root / ".agents" / "agents.toml").write_text(
            '', encoding="utf-8")
        cfg = load_config(
            self.root / ".agents" / "agents.toml", "parallel01")
        self.assertEqual(cfg.lockfile_rel, ".agents/parallel01/agents.lock")
        self.assertIn(".agents/parallel01/agents.lock", cfg.git_excludes)


if __name__ == "__main__":
    unittest.main()
