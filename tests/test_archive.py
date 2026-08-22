"""Tests for the archive subcommand: preconditions, the archive/restore round trip,
crash-resume idempotency at every interrupt point, and roster content."""
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest

from tests.engine_support import models_block
import zipfile
from pathlib import Path
from unittest.mock import patch

from assent import gitops, pathops
from assent.archive import (archive_all, archive_plan, archive_selected,
                            read_roster, restore_plan, _archive_dir,
                            _compress_plan, _write_roster, _zip_path)
from assent.config import load_config
from assent.lockfile import LockBusy, hold_lock
from assent.__main__ import _dispatch
from tests.link_support import make_directory_link, safe_rmtree


_VERIFY = 'python -c "raise SystemExit(0)"'


def _task_text(status: str = "DONE") -> str:
    return "\n".join((
        'title = "Archive task"',
        "deps = []",
        'model = "lite"',
        f"status = {json.dumps(status)}",
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


def _forget_worktree_metadata(path: Path) -> None:
    git_file = path / ".git"
    prefix = "gitdir: "
    text = git_file.read_text(encoding="utf-8").strip()
    if not text.startswith(prefix):
        raise AssertionError(f"unexpected worktree .git file: {text!r}")
    admin = Path(text.removeprefix(prefix))
    git_file.unlink()
    safe_rmtree(admin)


class TestArchive(unittest.TestCase):
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

        self.assent_dir = self.root / ".assent"
        self.plan_name = "plan01"
        self.tasks_dir = self.assent_dir / self.plan_name
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text(
            '[workflow]\ntask = [{ action = "focused_test" }]\n'
            + models_block(), encoding="utf-8")
        (self.tasks_dir / "t001_task.e.toml").write_text(
            _task_text(), encoding="utf-8")
        (self.tasks_dir / "t001_task.r.toml").write_text(
            'note = "journal"\n', encoding="utf-8")
        (self.tasks_dir / "assent.lock").write_text(
            f'plan = "{self.plan_name}"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, self.plan_name)
        self.container = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        safe_rmtree(self.container)
        subprocess.run(["git", "worktree", "prune"], cwd=self.root,
                       capture_output=True)

    def _archive(self, cfg=None) -> tuple[int, str]:
        cfg = cfg or self.cfg
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_plan(cfg)
        return code, output.getvalue()

    def _restore(self, cfg=None) -> tuple[int, str]:
        cfg = cfg or self.cfg
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = restore_plan(cfg)
        return code, output.getvalue()

    def _plan_name(self, name: str, status: str = "DONE") -> Path:
        plan_name = self.assent_dir / name
        plan_name.mkdir(exist_ok=True)
        (plan_name / "t001_task.e.toml").write_text(
            _task_text(status), encoding="utf-8")
        (plan_name / "assent.lock").write_text(
            f'plan = "{name}"\n', encoding="utf-8")
        return plan_name

    def _unmerged_source(self) -> tuple[Path, str]:
        worktree = gitops.ensure_worktree(self.root, self.plan_name)
        branch = gitops.ensure_branch(worktree, f"{self.plan_name}/")
        (worktree / "result.txt").write_text("work\n", encoding="utf-8")
        gitops.commit_all(worktree, "finish result")
        return worktree, branch

    def _finished_source(self, name: str) -> str:
        """Create a finished plan with a committed, not-yet-integrated source."""
        self._plan_name(name)
        worktree = gitops.ensure_worktree(self.root, name)
        branch = gitops.ensure_branch(worktree, f"{name}/")
        (worktree / f"{name}.txt").write_text("work\n", encoding="utf-8")
        gitops.commit_all(worktree, f"finish {name}")
        return branch

    def _integrate(self, branch: str) -> None:
        """Merge a source into the target the way an accepted plan leaves it."""
        _git(self.root, "merge", "--no-ff", branch, "-m", f"accept: {branch}")

    def _linked_accepted_source(self) -> tuple[Path, str, list[tuple[str, str]]]:
        """Build the incident shape: source links point at ignored main-tree data."""
        (self.root / ".gitignore").write_text(
            ".assent/\npkg/\nassets/\nlib/l10n/arb/\n", encoding="utf-8")
        (self.root / "lib" / "l10n").mkdir(parents=True)
        (self.root / "lib" / "l10n" / "app_en.arb").write_text(
            "{}\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore", "lib/l10n/app_en.arb")
        _git(self.root, "commit", "-m", "provision ignored archive targets")

        files = {
            "pkg": {"sentinel.txt": "pkg sentinel\n", "nested/data.txt": "pkg data\n"},
            "assets": {"sentinel.txt": "assets sentinel\n"},
            "lib/l10n/arb": {"app.arb": '{"keep": true}\n'},
        }
        for directory, contents in files.items():
            for relative, content in contents.items():
                target = self.root / directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        before = self._target_inventory(files)

        worktree = gitops.ensure_worktree(self.root, self.plan_name)
        branch = gitops.ensure_branch(worktree, f"{self.plan_name}/")
        make_directory_link(worktree / "pkg", self.root / "pkg")
        make_directory_link(worktree / "assets", self.root / "assets")
        make_directory_link(
            worktree / "lib" / "l10n" / "arb", self.root / "lib" / "l10n" / "arb")
        (worktree / "result.txt").write_text("accepted result\n", encoding="utf-8")
        gitops.commit_all(worktree, "finish linked archive source")
        self._integrate(branch)
        return worktree, branch, before

    def _target_inventory(self, files: dict[str, dict[str, str]]) -> list[tuple[str, str]]:
        return sorted(
            (str(path.relative_to(self.root)).replace("\\", "/"),
             hashlib.sha256(path.read_bytes()).hexdigest())
            for directory in files
            for path in (self.root / directory).rglob("*")
            if path.is_file())

    # ---- preconditions ---------------------------------------------------

    def test_unfinished_plan_is_refused(self) -> None:
        (self.tasks_dir / "t001_task.e.toml").write_text(
            _task_text("TODO"), encoding="utf-8")

        code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("Unfinished tasks: t001=TODO", output)
        self.assertFalse(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertTrue(self.tasks_dir.exists())
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_busy_lock_refuses_archive(self) -> None:
        with hold_lock(self.tasks_dir, self.plan_name):
            code, output = self._archive()

        self.assertEqual(code, 1)
        self.assertIn("a run is in progress", output)
        self.assertFalse(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertTrue(self.tasks_dir.exists())

    def test_plan_without_lock_file_archives(self) -> None:
        # A plan that predates the lock mechanism has no assent.lock; its absence
        # is proof nobody holds it, so archive acquires (creates) the lock and files
        # the plan rather than skipping it (the archive --all=0 incident).
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
        with zipfile.ZipFile(_zip_path(self.assent_dir, self.plan_name)) as zf:
            names = zf.namelist()
        self.assertNotIn("assent.lock", names)
        self.assertIn("t001_task.e.toml", names)

    def test_lock_is_held_across_archive_work(self) -> None:
        # While archive works, it holds the plan lock, so a concurrent
        # run/reject/rework acquisition is refused — closing the probe-then-act
        # TOCTOU window.  The probe happens mid-archive, during compression.
        import assent.archive as archive_mod
        original = archive_mod._compress_plan
        observed: dict[str, bool] = {}

        def spy(src_dir, tmp_zip):
            try:
                with hold_lock(self.tasks_dir, self.plan_name):
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
            self.root, f"{self.plan_name}/"))
        self.assertFalse(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertTrue(self.tasks_dir.exists())
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_foreign_zip_refuses_archive(self) -> None:
        zip_path = _zip_path(self.assent_dir, self.plan_name)
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
        zip_path = _zip_path(self.assent_dir, self.plan_name)
        self.assertTrue(zip_path.exists())
        self.assertFalse(self.tasks_dir.exists())
        roster = read_roster(self.assent_dir)
        self.assertEqual([e["plan"] for e in roster], [self.plan_name])
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

    def test_archive_recovers_a_recorded_partial_worktree_removal(self) -> None:
        worktree, branch = self._unmerged_source()
        self._integrate(branch)
        _forget_worktree_metadata(worktree)
        self.assertFalse(gitops.is_repo_worktree(self.root, worktree))
        gitops.adopt_worktree_removal(self.root, worktree)

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self.assertIn(f"{self.plan_name}: cleaned (worktree {worktree})", output)
        self.assertIn(f"{self.plan_name}: archived", output)
        self.assertFalse(worktree.exists())
        self.assertNotIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))

    def test_archive_retains_an_unowned_directory_at_the_fixed_path(self) -> None:
        path = gitops.worktree_path(self.root, self.plan_name)
        path.mkdir(parents=True)
        foreign = path / "keep.txt"
        foreign.write_text("not an Assent worktree\n", encoding="utf-8")
        branch = f"{self.plan_name}/integrated"
        _git(self.root, "branch", branch, "HEAD")

        code, output = self._archive()

        self.assertEqual(code, 1, output)
        self.assertIn("has no Assent removal evidence", output)
        self.assertIn("archive refuses to compress", output)
        self.assertEqual(foreign.read_text(encoding="utf-8"),
                         "not an Assent worktree\n")
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))

    def test_archive_detaches_main_tree_links_before_publishing(self) -> None:
        worktree, branch, before = self._linked_accepted_source()

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self.assertFalse(worktree.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"), [])
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": "", "nested/data.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)
        self._assert_fully_archived()

    def test_archive_all_detaches_main_tree_links_before_publishing(self) -> None:
        worktree, branch, before = self._linked_accepted_source()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)

        self.assertEqual(code, 0, output.getvalue())
        self.assertIn(f"{self.plan_name}: archived", output.getvalue())
        self.assertFalse(worktree.exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"), [])
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": "", "nested/data.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)
        self._assert_fully_archived()

    def test_archive_detachment_failure_keeps_targets_and_publication_state(self) -> None:
        worktree, branch, before = self._linked_accepted_source()

        with patch.object(pathops, "detach_directory_link",
                          side_effect=OSError("simulated detachment failure")):
            code, output = self._archive()

        self.assertEqual(code, 1, output)
        self.assertIn("linked target content was not touched", output)
        self.assertIn(str(worktree / "assets"), output)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.branches_with_prefix(
            self.root, f"{self.plan_name}/"))
        self.assertEqual(self._target_inventory({
            "pkg": {"sentinel.txt": "", "nested/data.txt": ""},
            "assets": {"sentinel.txt": ""},
            "lib/l10n/arb": {"app.arb": ""},
        }), before)
        self.assertFalse(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertEqual(read_roster(self.assent_dir), [])

    def test_restore_refuses_when_live_directory_exists(self) -> None:
        self._archive()
        # Recreate a live directory so restore must refuse rather than overwrite.
        self.tasks_dir.mkdir()
        (self.tasks_dir / "keep.txt").write_text("mine\n", encoding="utf-8")

        code, output = self._restore()

        self.assertEqual(code, 1)
        self.assertIn("already exists", output)
        self.assertTrue(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertEqual((self.tasks_dir / "keep.txt").read_text(encoding="utf-8"),
                         "mine\n")

    def test_restore_refuses_without_archive(self) -> None:
        shutil.rmtree(self.tasks_dir)

        code, output = self._restore()

        self.assertEqual(code, 1)
        self.assertIn("no archive", output)

    # ---- crash-resume idempotency ---------------------------------------

    def _assert_fully_archived(self) -> None:
        self.assertTrue(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertFalse(self.tasks_dir.exists())
        self.assertEqual(
            [e["plan"] for e in read_roster(self.assent_dir)], [self.plan_name])

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
            {"plan": self.plan_name, "archived_at": "2026-07-25T00:00:00+00:00"}])
        self.assertFalse(_zip_path(self.assent_dir, self.plan_name).exists())

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self._assert_fully_archived()

    def test_resume_after_publish_before_delete(self) -> None:
        # Roster committed and zip published, but the live directory not yet
        # deleted (crash between rename and delete).
        _write_roster(self.assent_dir, [
            {"plan": self.plan_name, "archived_at": "2026-07-25T00:00:00+00:00"}])
        zip_path = _zip_path(self.assent_dir, self.plan_name)
        _compress_plan(self.tasks_dir, zip_path)
        self.assertTrue(self.tasks_dir.exists())

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self._assert_fully_archived()

    def test_a_new_plan_reusing_an_archived_name_is_refused_not_deleted(self) -> None:
        # The roster and zip belong to an older plan; the live directory is a
        # different plan that happens to reuse its name. Treating that as an
        # interrupted archive would delete work no archive ever captured.
        _write_roster(self.assent_dir, [
            {"plan": self.plan_name, "archived_at": "2026-07-25T00:00:00+00:00"}])
        zip_path = _zip_path(self.assent_dir, self.plan_name)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("t001_older_objective.e.toml", _task_text())

        code, output = self._archive()

        self.assertEqual(code, 1, output)
        self.assertIn("reusing an archived name", output)
        self.assertIn("t001_task.e.toml", output)
        self.assertTrue(self.tasks_dir.is_dir())
        self.assertTrue((self.tasks_dir / "t001_task.e.toml").is_file())
        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(zf.namelist(), ["t001_older_objective.e.toml"])

    def test_resume_during_delete(self) -> None:
        # Roster committed, zip published, live directory half-removed.
        _write_roster(self.assent_dir, [
            {"plan": self.plan_name, "archived_at": "2026-07-25T00:00:00+00:00"}])
        zip_path = _zip_path(self.assent_dir, self.plan_name)
        _compress_plan(self.tasks_dir, zip_path)
        (self.tasks_dir / "t001_task.r.toml").unlink()  # partial deletion

        code, output = self._archive()

        self.assertEqual(code, 0, output)
        self._assert_fully_archived()

    # ---- --all -----------------------------------------------------------

    def test_archive_all_skips_ineligible_without_failing(self) -> None:
        self._plan_name("plan02", status="TODO")  # ineligible: unfinished

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)
        text = output.getvalue()

        self.assertEqual(code, 0, text)
        self.assertIn("1 archived, 1 skipped, 0 error(s)", text)
        self.assertTrue(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertFalse(_zip_path(self.assent_dir, "plan02").exists())
        self.assertEqual(
            [e["plan"] for e in read_roster(self.assent_dir)], [self.plan_name])

    def test_archive_all_files_only_the_work_cleanup_proves_safe(self) -> None:
        """A partly accepted batch leaves a mixed ``.assent/``, and each plan is
        still judged on its own evidence.

        Nothing here tells archive why a source was left unaccepted -- a conflict
        skipped during batch filtering and a plan nobody has accepted yet look
        identical to it, which is the point: eligibility is per plan, and every
        safety-driven retention stays a visible skip rather than an error.
        """
        self._integrate(self._finished_source("independent"))
        self._finished_source("conflicting")
        self._integrate(self._finished_source("upstream"))
        self._finished_source("zdependent")
        (self.assent_dir / "zdependent" / "_plan_deps.toml").write_text(
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
        # The roster records archived plans and nothing about the skips.
        roster = read_roster(self.assent_dir)
        self.assertEqual(sorted(entry["plan"] for entry in roster),
                         ["independent", self.plan_name])
        for entry in roster:
            self.assertEqual(sorted(entry),
                             ["archived_at", "main_tip", "plan"])

    def _orphaned_temporary_branches(self) -> tuple[str, str]:
        """One published and one superseded orphan in Assent's temporary namespaces."""
        target = _git(self.root, "branch", "--show-current")
        published = "assent-integration/batch/aaaa"
        _git(self.root, "branch", published, "HEAD")
        superseded = "assent-reconcile/plan01"
        _git(self.root, "checkout", "-b", superseded)
        (self.root / "abandoned.txt").write_text("abandoned\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "abandoned reconcile work")
        _git(self.root, "checkout", target)
        return published, superseded

    def _temporary_branches(self) -> list[str]:
        return sorted(branch
                      for prefix in gitops.TEMPORARY_BRANCH_PREFIXES
                      for branch in gitops.branches_with_prefix(self.root, prefix))

    def test_archive_all_sweeps_orphaned_temporary_branches(self) -> None:
        """``archive --all`` owns the global namespace exactly as plan-less ``clean``
        does, and inherits the sweep instead of reimplementing it."""
        published, superseded = self._orphaned_temporary_branches()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)
        text = output.getvalue()

        self.assertEqual(code, 0, text)
        self.assertEqual(self._temporary_branches(), [])
        self.assertIn(f"  branch {published}: cleaned (published)", text)
        self.assertIn(f"  branch {superseded}: cleaned (superseded)", text)
        # The sweep runs once, after the per-plan loop and outside every
        # per-plan integration-lock hold.
        self.assertEqual(text.count("orphaned temporary branches:"), 1)
        self.assertLess(text.index(f"{self.plan_name}: archived"),
                        text.index("orphaned temporary branches:"))
        self.assertIn("1 archived, 0 skipped, 0 error(s)", text)

    def test_archive_all_delegates_the_sweep_to_clean(self) -> None:
        """The one call site delegates; archive keeps no sweep of its own."""
        self._orphaned_temporary_branches()

        output = io.StringIO()
        with patch("assent.archive.sweep_orphaned_temporary_branches",
                   return_value=0) as sweep, contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)

        self.assertEqual(code, 0, output.getvalue())
        sweep.assert_called_once()
        # Patching out the single delegating call leaves no second implementation
        # inside archive.py that could still remove the refs.
        self.assertEqual(len(self._temporary_branches()), 2)

    def test_archive_all_without_orphans_prints_nothing_new(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)
        text = output.getvalue()

        self.assertEqual(code, 0, text)
        self.assertNotIn("orphaned temporary branches", text)

    def test_archive_all_reports_a_refused_orphan_as_a_failure(self) -> None:
        published, superseded = self._orphaned_temporary_branches()
        checkout = self.root.parent / f"{self.root.name}.checkout"
        self.addCleanup(safe_rmtree, checkout)
        _git(self.root, "worktree", "add", str(checkout), published)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_all(str(self.config_path), self.assent_dir)
        text = output.getvalue()

        self.assertEqual(code, 1, text)
        self.assertEqual(self._temporary_branches(), [published])
        self.assertIn(f"  branch {published}: refused (checked out in", text)
        self.assertIn(f"  branch {superseded}: cleaned (superseded)", text)
        # The refusal is the sweep's, not the plan's: the plan still archived.
        self.assertIn("1 archived, 0 skipped, 0 error(s)", text)

    # ---- explicit multi-plan selection ---------------------------------

    def test_selected_archive_reports_an_ineligible_plan_as_a_failure(self) -> None:
        """``--all`` skips what it cannot archive; a named plan is a request."""
        self._plan_name("plan02", status="TODO")  # ineligible: unfinished

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_selected(str(self.config_path),
                                    [self.plan_name, "plan02"])
        text = output.getvalue()

        self.assertEqual(code, 1, text)
        self.assertIn("plan02: skipped", text)
        self.assertIn("archive summary: 1 archived, 1 not archived.", text)
        # The refusal does not stop the plans selected alongside it.
        self.assertTrue(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertFalse(_zip_path(self.assent_dir, "plan02").exists())

    def test_selected_archive_audits_all_names_before_first_archive(self) -> None:
        output = io.StringIO()
        with patch("assent.archive._archive_one",
                   side_effect=AssertionError("archive started")) as archive_one, \
                contextlib.redirect_stdout(output):
            code = archive_selected(
                str(self.config_path), [self.plan_name, "missing", "also_missing"])

        self.assertEqual(code, 1)
        archive_one.assert_not_called()
        self.assertIn("missing, also_missing", output.getvalue())
        self.assertFalse(_zip_path(self.assent_dir, self.plan_name).exists())
        self.assertFalse((self.assent_dir / "missing").exists())

    def test_direct_archive_of_missing_plan_is_controlled(self) -> None:
        missing = load_config(self.config_path, "missing")
        code, output = self._archive(missing)
        self.assertEqual(code, 1)
        self.assertIn("unresolved", output)
        self.assertFalse(missing.tasks_dir.exists())

    def test_selected_archive_files_every_eligible_plan(self) -> None:
        self._plan_name("plan02")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = archive_selected(str(self.config_path),
                                    ["plan02", self.plan_name])
        text = output.getvalue()

        self.assertEqual(code, 0, text)
        self.assertIn("archive summary: 2 archived, 0 not archived.", text)
        self.assertEqual(
            sorted(entry["plan"] for entry in read_roster(self.assent_dir)),
            ["plan01", "plan02"])

    # ---- CLI argument guards --------------------------------------------

    def test_bare_archive_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive"])

    def test_plan_and_all_together_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive", "plan01", "--all"])

    def test_restore_with_all_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive", "--restore", "--all"])

    def test_restore_without_plan_is_a_parser_error(self) -> None:
        with self.assertRaises(SystemExit):
            _dispatch(["archive", "--restore"])

    def test_restore_stays_single_plan_even_with_a_remainder(self) -> None:
        for argv in (["archive", "plan01", "plan02", "--restore"],
                     ["archive", "plan01", "...", "--restore"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                _dispatch(argv)


if __name__ == "__main__":
    unittest.main()
