"""Plan file lock tests.

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

from assent import AssentError
from assent.config import load_config
from assent.gitops import git_common_dir
from assent.lockfile import (
    INTEGRATION_LOCK_NAME, LOCK_NAME, LockBusy, hold_integration_lock,
    hold_lock)

# Importing assent requires the repo root on the path; the subprocess uses it as cwd.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# Subprocess: after acquiring the lock, print a LOCKED line to notify the parent, then
# block on reading stdin while holding the lock; the parent closing its stdin releases
# the lock and lets it exit.
_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from assent.lockfile import hold_lock
    with hold_lock(Path(sys.argv[1]), sys.argv[2]):
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        sys.stdin.readline()
    """
)

_INTEGRATION_HOLDER = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from assent.lockfile import hold_integration_lock
    with hold_integration_lock(Path(sys.argv[1])):
        sys.stdout.write("LOCKED\\n")
        sys.stdout.flush()
        sys.stdin.readline()
    """
)


class TestHoldLock(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tasks_dir = self.root / ".assent" / "parallel01"
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
        try:
            proc.wait(timeout=10)
        finally:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()

    def _release_holder(self, proc: subprocess.Popen) -> None:
        proc.stdin.close()  # subprocess readline gets EOF -> exits the with block -> releases the lock
        self.assertEqual(proc.wait(timeout=10), 0)

    def test_second_run_blocked_with_pid_and_plan(self):
        """While the subprocess holds the lock, a second acquire on the same plan fails immediately, with a message including the holder's PID and plan name."""
        proc = self._start_holder()
        with self.assertRaises(LockBusy) as ctx:
            with hold_lock(self.tasks_dir, "parallel01"):
                pass
        msg = str(ctx.exception)
        self.assertIn("parallel01", msg)
        self.assertIn(str(proc.pid), msg)

    def test_missing_plan_is_not_created_for_a_lock(self):
        missing = self.root / ".assent" / "missing"
        with self.assertRaises(AssentError):
            with hold_lock(missing, "missing"):
                pass
        self.assertFalse(missing.exists())

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

    def test_leftover_lockfile_is_not_mistaken_for_an_active_lock(self):
        """A file left behind by a forcibly terminated run, with a PID that is
        not this process, still grants the lock: ownership is the OS handle, and
        the recorded PID is diagnostics only."""
        path = self.tasks_dir / LOCK_NAME
        path.write_text(
            'pid = 999999\nstarted_at = "2026-01-01T00:00:00+00:00"\n'
            'plan = "parallel01"\n', encoding="utf-8")
        with hold_lock(self.tasks_dir, "parallel01"):
            pass
        self.assertTrue(path.is_file())

    def test_killed_holder_frees_the_lock_for_the_next_run_immediately(self):
        """The scheduler's forced tree termination is the backstop for a child
        that will not stop on its own, so the run it kills must not leave the
        plan locked against the next one."""
        proc = self._start_holder()
        proc.kill()
        self.assertNotEqual(proc.wait(timeout=10), 0)  # not a clean release
        with hold_lock(self.tasks_dir, "parallel01"):
            pass
        self.assertTrue((self.tasks_dir / LOCK_NAME).is_file())

    def test_git_excludes_contains_lockfile(self):
        """Config.git_excludes contains the lock file's relative path (not tracked by git, not part of clean/scope checks)."""
        (self.root / ".assent" / "assent.toml").write_text(
            '', encoding="utf-8")
        cfg = load_config(
            self.root / ".assent" / "assent.toml", "parallel01")
        self.assertEqual(cfg.lockfile_rel, ".assent/parallel01/assent.lock")
        self.assertIn(".assent/parallel01/assent.lock", cfg.git_excludes)


class TestIntegrationLock(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(
            ["git", "init"], cwd=self.root, check=True,
            capture_output=True, encoding="utf-8")
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()

    def _start_holder(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            [sys.executable, "-c", _INTEGRATION_HOLDER, str(self.assent_dir)],
            cwd=str(_REPO_ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            encoding="utf-8")
        self.addCleanup(self._cleanup_holder, proc)
        self.assertEqual(proc.stdout.readline().strip(), "LOCKED")
        return proc

    def _cleanup_holder(self, proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        finally:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            if proc.stdout is not None and not proc.stdout.closed:
                proc.stdout.close()

    def test_second_integration_is_rejected(self):
        proc = self._start_holder()
        with self.assertRaises(LockBusy) as caught:
            with hold_integration_lock(self.assent_dir):
                pass
        self.assertIn(str(proc.pid), str(caught.exception))

    def test_release_allows_reacquire(self):
        proc = self._start_holder()
        proc.stdin.close()
        self.assertEqual(proc.wait(timeout=10), 0)
        with hold_integration_lock(self.assent_dir):
            pass

    def test_stale_file_does_not_block(self):
        path = git_common_dir(self.root) / INTEGRATION_LOCK_NAME
        path.write_text("pid = 999999\n", encoding="utf-8")
        with hold_integration_lock(self.assent_dir):
            pass
        self.assertTrue(path.is_file())

    def test_different_management_directories_share_repository_lock(self):
        proc = self._start_holder()
        alternate = self.root / ".alternate-assent"
        alternate.mkdir()
        with self.assertRaises(LockBusy):
            with hold_integration_lock(alternate):
                pass
        self.assertTrue(
            (git_common_dir(self.root) / INTEGRATION_LOCK_NAME).is_file())
        self.assertFalse((alternate / INTEGRATION_LOCK_NAME).exists())


if __name__ == "__main__":
    unittest.main()
