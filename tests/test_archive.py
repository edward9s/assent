"""Tests for the archive subcommand: preconditions, the archive/restore round trip,
crash-resume idempotency at every interrupt point, and roster content."""
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from assent import gitops
from assent.archive import (archive_all, archive_folder, read_roster,
                            restore_folder, _archive_dir, _write_roster,
                            _zip_path)
from assent.config import load_config
from assent.lockfile import LockBusy, hold_lock
from assent.__main__ import _dispatch


_VERIFY = 'python -c "raise SystemExit(0)"'


def _task_text(status: str = "DONE") -> str:
    return "\n".join((
        'title = "Archive task"',
        "deps = []",
        'model = "lite"',
        f"status = {json.dumps(status)}",
        'scope = ["src/"]',
        f"verify = {json.dumps(_VERIFY)}",
        'goal = "Keep archival safe."',
        'acceptance = "Archival preserves the plan."',
        "",
    ))


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class TestArchive(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "README.md").write_text("init\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "init")

        self.assent_dir = self.root / ".assent"
        self.folder = "plan01"
        self.tasks_dir = self.assent_dir / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.tasks_dir / "t001_task.e.toml").write_text(
            _task_text(), encoding="utf-8")
        (self.tasks_dir / "t001_task.r.toml").write_text(
            'note = "journal"\n', encoding="utf-8")
        (self.tasks_dir / "assent.lock").write_text(
            f'folder = "{self.folder}"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.folder)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        shutil.rmtree(self.container, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _archive(self, cfg=None) -> tuple[int, str]:
        cfg = cfg or self.cfg
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_folder(cfg)
        return code, output.getvalue()

    def _restore(self, cfg=None) -> tuple[int, str]:
        cfg = cfg or self.cfg
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = restore_folder(cfg)
        return code, output.getvalue()

    def _folder(self, name: str, status: str = "DONE") -> Path:
        folder = self.assent_dir / name
        folder.mkdir(exist_ok=True)
        (folder / "t001_task.e.toml").write_text(
            _task_text(status), encoding="utf-8")
        (folder / "assent.lock").write_text(
            f'folder = "{name}"\n', encoding="utf-8")
        return folder

    def _unmerged_source(self) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, self.folder)
        branch = gitops.ensure_branch(worktree, f"{self.folder}/")
        (worktree / "result.txt").write_text("work\n", encoding="utf-8")
        gitops.commit_all(worktree, "finish result")
        return worktree, branch

    def _finished_source(self, name: str) -> str:
        """Create a finished folder with a committed, not-yet-integrated source."""
        self._folder(name)
        worktree = gitops.ensure_worktree(self.root, name)
        branch = gitops.ensure_branch(worktree, f"{name}/")
        (worktree / f"{name}.txt").write_text("work\n", encoding="utf-8")
        gitops.commit_all(worktree, f"finish {name}")
        return branch

    def _integrate(self, branch: str) -> None:
        """Merge a source into the target the way an accepted folder leaves it."""
        _git(self.root, "merge", "--no-ff", branch, "-m", f"accept: {branch}")

    # ---- preconditions ---------------------------------------------------

    def test_unfinished_folder_is_refused(self) -> None:
        (self.tasks_dir / "t001_task.e.toml").write_text(
            _task_text("TODO"), encoding="utf-8")

        code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("Unfinished tasks: t001=TODO", output)
        self.assertFalse(_zip_path(self.assent_dir, self.folder).exists())
        self.assertTrue(self.tasks_dir.exists())
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_busy_lock_refuses_archive(self) -> None:
        with hold_lock(self.tasks_dir, self.folder):
            code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("a run is in progress", output)
        self.assertFalse(_zip_path(self.assent_dir, self.folder).exists())
        self.assertTrue(self.tasks_dir.exists())

    def test_folder_without_lock_file_archives(self) -> None:
        # A folder that predates the lock mechanism has no assent.lock; its absence
        # is proof nobody holds it, so archive acquires (creates) the lock and files
        # the folder rather than skipping it (the archive --all=0 incident).
        (self.tasks_dir / "assent.lock").unlink()

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self.assertIn("archived", output)
        self._assert_fully_archived()

    def test_zip_excludes_the_lock_file(self) -> None:
        # assent.lock is a runtime artifact and must never enter the archive zip,
        # whether it was already present or created by archive taking the lock.
        code, output = self._archive()

        self.assertEqual(code, 0, output)
        with zipfile.ZipFile(_zip_path(self.assent_dir, self.folder)) as zf:
            names = zf.namelist()
        self.assertNotIn("assent.lock", names)
        self.assertIn("t001_task.e.toml", names)

    def test_lock_is_held_across_archive_work(self) -> None:
        # While archive works, it holds the folder lock, so a concurrent
        # run/reject/rework acquisition is refused — closing the probe-then-act
        # TOCTOU window.  The probe happens mid-archive, during compression.
        import assent.archive as archive_mod
        original = archive_mod._compress_plan
        observed: dict[str, bool] = {}

        def spy(src_dir, tmp_zip):
            try:
                with hold_lock(self.tasks_dir, self.folder):
                    observed["acquired"] = True
            except LockBusy:
                observed["blocked"] = True
            return original(src_dir, tmp_zip)

        archive_mod._compress_plan = spy
        self.addCleanup(setattr, archive_mod, "_compress_plan", original)

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self.assertTrue(observed.get("blocked"))
        self.assertNotIn("acquired", observed)
        self._assert_fully_archived()

    def test_unintegrated_source_refuses_archive(self) -> None:
        worktree, branch = self._unmerged_source()

        code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("not yet integrated", output)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.folder}/"))
        self.assertFalse(_zip_path(self.assent_dir, self.folder).exists())
        self.assertTrue(self.tasks_dir.exists())
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_foreign_zip_refuses_archive(self) -> None:
        zip_path = _zip_path(self.assent_dir, self.folder)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(b"not really a zip")

        code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("foreign archive", output)
        self.assertTrue(self.tasks_dir.exists())
        self.assertEqual(zip_path.read_bytes(), b"not really a zip")
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_malformed_roster_fails_closed(self) -> None:
        (self.assent_dir / "_archived.toml").write_text(
            "archived = 3\n", encoding="utf-8")

        code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("archive error", output)
        self.assertTrue(self.tasks_dir.exists())

    # ---- happy path and round trip --------------------------------------

    def test_archive_then_restore_round_trip(self) -> None:
        head = _git(self.root, "rev-parse", "HEAD")

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self.assertIn("archived", output)
        zip_path = _zip_path(self.assent_dir, self.folder)
        self.assertTrue(zip_path.exists())
        self.assertFalse(self.tasks_dir.exists())
        roster = read_roster(self.assent_dir)
        self.assertEqual([e["folder"] for e in roster], [self.folder])
        self.assertIn("archived_at", roster[0])
        self.assertEqual(roster[0].get("main_tip"), head)
        with zipfile.ZipFile(zip_path) as zf:
            self.assertIn("t001_task.e.toml", zf.namelist())
            self.assertIn("t001_task.r.toml", zf.namelist())

        code, output = self._restore()

        self.assertEqual(code, 0, output)
        self.assertIn("restored", output)
        self.assertTrue(self.tasks_dir.exists())
        self.assertEqual(
            (self.tasks_dir / "t001_task.e.toml").read_text(encoding="utf-8"),
            _task_text())
        self.assertFalse(zip_path.exists())
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_restore_refuses_when_live_directory_exists(self) -> None:
        self._archive()
        # Recreate a live directory so restore must refuse rather than overwrite.
        self.tasks_dir.mkdir()
        (self.tasks_dir / "keep.txt").write_text("mine\n", encoding="utf-8")

        code, output = self._restore()

        self.assertEqual(code, 1)
        self.assertIn("already exists", output)
        self.assertTrue(_zip_path(self.assent_dir, self.folder).exists())
        self.assertEqual((self.tasks_dir / "keep.txt").read_text(encoding="utf-8"),
                         "mine\n")

    def test_restore_refuses_without_archive(self) -> None:
        shutil.rmtree(self.tasks_dir)

        code, output = self._restore()

        self.assertEqual(code, 1)
        self.assertIn("no archive", output)

    # ---- crash-resume idempotency ---------------------------------------

    def _assert_fully_archived(self) -> None:
        self.assertTrue(_zip_path(self.assent_dir, self.folder).exists())
        self.assertFalse(self.tasks_dir.exists())
        self.assertEqual(
            [e["folder"] for e in read_roster(self.assent_dir)], [self.folder])

    def test_rerun_after_completion_is_idempotent(self) -> None:
        self._archive()
        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self.assertIn("already archived", output)
        self._assert_fully_archived()

    def test_resume_after_register_before_publish(self) -> None:
        # Roster committed but the final zip not yet published (crash between
        # register and rename); the live directory is still present.
        _write_roster(self.assent_dir, [
            {"folder": self.folder, "archived_at": "2026-07-25T00:00:00+00:00"}])
        self.assertFalse(_zip_path(self.assent_dir, self.folder).exists())

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self._assert_fully_archived()

    def test_resume_after_publish_before_delete(self) -> None:
        # Roster committed and zip published, but the live directory not yet
        # deleted (crash between rename and delete).
        _write_roster(self.assent_dir, [
            {"folder": self.folder, "archived_at": "2026-07-25T00:00:00+00:00"}])
        zip_path = _zip_path(self.assent_dir, self.folder)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("t001_task.e.toml", _task_text())
        self.assertTrue(self.tasks_dir.exists())

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self._assert_fully_archived()

    def test_resume_during_delete(self) -> None:
        # Roster committed, zip published, live directory half-removed.
        _write_roster(self.assent_dir, [
            {"folder": self.folder, "archived_at": "2026-07-25T00:00:00+00:00"}])
        zip_path = _zip_path(self.assent_dir, self.folder)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("t001_task.e.toml", _task_text())
        (self.tasks_dir / "t001_task.r.toml").unlink()  # partial deletion

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self._assert_fully_archived()

    # ---- --all -----------------------------------------------------------

    def test_archive_all_skips_ineligible_without_failing(self) -> None:
        self._folder("plan02", status="TODO")  # ineligible: unfinished

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)
        text = output.getvalue()

        self.assertEqual(code, 0, text)
        self.assertIn("1 archived, 1 skipped, 0 error(s)", text)
        self.assertTrue(_zip_path(self.assent_dir, self.folder).exists())
        self.assertFalse(_zip_path(self.assent_dir, "plan02").exists())
        self.assertEqual(
            [e["folder"] for e in read_roster(self.assent_dir)], [self.folder])

    def test_archive_all_files_only_the_work_cleanup_proves_safe(self) -> None:
        """A partly accepted batch leaves a mixed ``.assent/``, and each folder is
        still judged on its own evidence.

        Nothing here tells archive why a source was left unaccepted -- a conflict
        skipped during batch filtering and a folder nobody has accepted yet look
        identical to it, which is the point: eligibility is per folder, and every
        safety-driven retention stays a visible skip rather than an error.
        """
        self._integrate(self._finished_source("independent"))
        self._finished_source("conflicting")
        self._integrate(self._finished_source("upstream"))
        self._finished_source("zdependent")
        (self.assent_dir / "zdependent" / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)
        text = output.getvalue()

        self.assertEqual(code, 0, text)
        self.assertIn("independent: archived", text)
        self.assertIn("conflicting: skipped", text)
        self.assertIn("zdependent: skipped", text)
        # upstream is itself integrated, yet its source is the evidence its
        # unaccepted dependent still needs, so cleanup retains it.
        self.assertIn("upstream: skipped", text)
        self.assertIn("dependent source evidence is still required", text)
        self.assertIn("dependent zdependent:", text)
        self.assertIn("2 archived, 3 skipped, 0 error(s)", text)

        self.assertTrue(_zip_path(self.assent_dir, "independent").exists())
        self.assertEqual(
            gitops.branches_with_prefix(self.root, "independent/"), [])
        for retained in ("conflicting", "upstream", "zdependent"):
            self.assertFalse(_zip_path(self.assent_dir, retained).exists())
            self.assertTrue((self.assent_dir / retained).is_dir())
            self.assertNotEqual(
                gitops.branches_with_prefix(self.root, f"{retained}/"), [])
        # The roster records archived folders and nothing about the skips.
        roster = read_roster(self.assent_dir)
        self.assertEqual(sorted(entry["folder"] for entry in roster),
                         ["independent", self.folder])
        for entry in roster:
            self.assertEqual(sorted(entry),
                             ["archived_at", "folder", "main_tip"])

    # ---- CLI argument guards --------------------------------------------

    def test_bare_archive_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive"])

    def test_folder_and_all_together_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive", "plan01", "--all"])

    def test_restore_with_all_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive", "--restore", "--all"])

    def test_restore_without_folder_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive", "--restore"])


if __name__ == "__main__":
    unittest.main()
