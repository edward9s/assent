"""Behavioral tests for the sequential ``assent accept --all`` chain:
finished-folder selection, dependency order, verify-then-accept interleaving,
fail-closed chain stop, and idempotent rerun.

The batch release path -- how ``--all`` chooses between releasing a fresh batch
receipt and this per-folder chain, and everything the explicit selected
``accept A B`` release requires -- is covered by tests/test_batch_accept.py,
which reuses the repository fixture defined here.

CLI argument-combination tests for ``--all`` live in tests/test_accept_cli.py;
this module exercises ``accept_all`` directly against disposable local
repositories, the same style ``tests/test_accept.py`` uses for
``accept_folder``.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from assent import gitops, verification
from assent.accept import accept_folder
from assent.batch_accept import accept_all
from assent.clean import clean_folder
from assent.config import load_config

_VERIFY_OK = "raise SystemExit(0)\n"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class AcceptAllRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent accept all test "))
        self.root = self.parent / "repository"
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

        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        self._write_verify(_VERIFY_OK)

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

    def _write_verify(self, text: str) -> None:
        (self.assent_dir / "verify.py").write_text(text, encoding="utf-8")

    def _write_task(self, folder: str, task_id: str = "t001",
                    status: str = "DONE") -> Path:
        tasks_dir = self.assent_dir / folder
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{task_id}_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "core"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            'verify = "python .assent/verify.py"\n'
            'goal = "Complete the task."\n'
            'acceptance = "Verification passes."\n',
            encoding="utf-8")
        return path

    def _write_after(self, folder: str, after: tuple[str, ...]) -> None:
        values = ", ".join(f'"{item}"' for item in after)
        (self.assent_dir / folder / "_folder.toml").write_text(
            f"after = [{values}]\n", encoding="utf-8")

    def _make_source(self, folder: str, *, filename: str | None = None,
                     content: str = "result\n",
                     start_snapshot: str | None = None) -> tuple[Path, str, str]:
        filename = filename or f"{folder}.txt"
        worktree = gitops.ensure_worktree(self.root, folder, start_snapshot)
        branch = gitops.ensure_branch(worktree, f"{folder}/")
        (worktree / filename).write_text(content, encoding="utf-8")
        gitops.commit_all(worktree, f"finish {folder}")
        return worktree, branch, gitops.branch_tip(self.root, branch)

    def _config(self, folder: str):
        return load_config(self.config_path, folder)

    def _accept_all(self) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = accept_all(str(self.config_path), self.assent_dir)
        return code, output.getvalue()

    def _head(self, ref: str = "HEAD") -> str:
        return _git(self.root, "rev-parse", ref)

    def _accept_subjects(self) -> list[str]:
        subjects = _git(self.root, "log", "--format=%s", "--reverse").splitlines()
        return [subject for subject in subjects if subject.startswith("accept(")]

    def _write_receipt(self, folder: str, *, status: str = "PASSED"
                       ) -> verification.VerificationReceipt:
        cfg = self._config(folder)
        target_tip = self._head()
        branches = gitops.folder_branches(self.root, folder)
        self.assertEqual(len(branches), 1)
        source_tip = gitops.branch_tip(self.root, branches[0])
        with gitops.temporary_integration_worktree(
                self.root, folder, target_tip) as (candidate, _branch):
            outcome = gitops.merge_no_ff(
                candidate, source_tip, f"prepare receipt for {folder}")
            self.assertTrue(outcome.ok, outcome.conflicts)
            tree = gitops.tree_of(candidate, "HEAD")
        digest = verification.verifier_digest(cfg)
        receipt = verification.VerificationReceipt(
            version=verification.RECEIPT_VERSION,
            status=status,
            source_tip=source_tip,
            target_tip=target_tip,
            integration_tree=tree,
            verify_script_sha256=digest,
            verify_command=verification.VERIFY_COMMAND,
            exit_code=0 if status == "PASSED" else 7,
            completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            failure_summary="" if status == "PASSED" else "simulated failure",
        )
        verification.write_receipt(
            verification.receipt_path(cfg), receipt, self.root)
        return receipt


class TestSelection(AcceptAllRepositoryCase):
    def test_no_task_folder_at_all_exits_zero(self) -> None:
        code, output = self._accept_all()
        self.assertEqual(code, 0, output)
        self.assertIn("no work folder with a task file found", output)

    def test_unfinished_folders_are_skipped_not_errors(self) -> None:
        for status in ("TODO", "WIP", "BLOCKED"):
            self._write_task(f"folder-{status.lower()}", status=status)

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        for status in ("TODO", "WIP", "BLOCKED"):
            self.assertIn(f"skip folder-{status.lower()}", output)
        self.assertIn("no finished work folder to accept", output)

    def test_bad_folder_dependency_graph_fails_closed(self) -> None:
        self._write_task("orphan")
        (self.assent_dir / "orphan" / "_folder.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertIn("folder dependency graph is invalid", output)


class TestOrderingAndPublication(AcceptAllRepositoryCase):
    def test_dependency_order_and_lexicographic_tie_break_publish_all(self) -> None:
        for folder in ("aaa", "alpha", "beta"):
            self._write_task(folder)
        self._write_after("beta", ("alpha",))
        self._make_source("aaa")
        _, _, alpha_tip = self._make_source("alpha")
        # Stacked on alpha's still-unaccepted tip: a real downstream task
        # session would build its worktree the same way (resolve_folder_base).
        self._make_source("beta", start_snapshot=alpha_tip)

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertEqual(self._accept_subjects(), [
            "accept(aaa): integrate into trunk",
            "accept(alpha): integrate into trunk",
            "accept(beta): integrate into trunk",
        ])
        self.assertIn("accepted:  aaa, alpha, beta", output)

    def test_fresh_receipt_skips_full_verify_but_stale_receipt_refreshes(self) -> None:
        counter = self.parent / "verify_runs.log"
        counter.write_text("", encoding="utf-8")
        self._write_verify(
            "import pathlib\n"
            f"pathlib.Path({str(counter)!r}).open('a', encoding='utf-8').write('run\\n')\n"
            "raise SystemExit(0)\n")
        self._write_task("fresh")
        self._write_task("stale")
        self._make_source("fresh")
        self._make_source("stale")
        self._write_receipt("fresh")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertEqual(counter.read_text(encoding="utf-8").count("run\n"), 1)
        self.assertIn("existing PASSED receipt is fresh", output)
        self.assertEqual(self._accept_subjects(), [
            "accept(fresh): integrate into trunk",
            "accept(stale): integrate into trunk",
        ])


class TestFailClosedChain(AcceptAllRepositoryCase):
    def test_conflict_stops_chain_but_keeps_prior_accepts_and_leaves_remaining_untouched(
            self) -> None:
        for folder in ("alpha", "beta", "gamma"):
            self._write_task(folder)
        self._make_source("alpha", filename="shared.txt", content="alpha\n")
        self._make_source("beta", filename="shared.txt", content="beta\n")
        self._make_source("gamma", filename="gamma.txt", content="gamma\n")

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._accept_subjects(),
                         ["accept(alpha): integrate into trunk"])
        self.assertIn("failed:    beta", output)
        self.assertIn("remaining: gamma", output)


class TestIdempotentRerun(AcceptAllRepositoryCase):
    def test_rerun_after_full_acceptance_is_a_noop(self) -> None:
        self._write_task("solo")
        self._make_source("solo")
        first_code, first_output = self._accept_all()
        self.assertEqual(first_code, 0, first_output)
        head_after_first = self._head()

        second_code, second_output = self._accept_all()

        self.assertEqual(second_code, 0, second_output)
        self.assertEqual(self._head(), head_after_first)
        self.assertIn("already accepted", second_output)
        self.assertIn("accepted:  solo", second_output)

    def test_multiple_already_merged_folders_noop_then_pending_folder_proceeds(
            self) -> None:
        """Reproduces the acceptall01 incident inside ``--all``: folders

        already accepted before main advanced further must each resolve as
        idempotent no-ops, and the chain must still continue on to accept a
        genuinely pending folder afterwards.
        """
        for folder in ("alpha", "beta"):
            self._write_task(folder)
            self._make_source(folder)
        first_code, first_output = self._accept_all()
        self.assertEqual(first_code, 0, first_output)
        published = self._head()

        (self.root / "advance.txt").write_text("advance\n", encoding="utf-8")
        _git(self.root, "add", "advance.txt")
        _git(self.root, "commit", "-m", "advance target after acceptance")
        advanced = self._head()
        self.assertNotEqual(advanced, published)

        self._write_task("gamma")
        self._make_source("gamma")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("already accepted", output)
        self.assertIn("accepted:  alpha, beta, gamma", output)
        self.assertEqual(self._accept_subjects(), [
            "accept(alpha): integrate into trunk",
            "accept(beta): integrate into trunk",
            "accept(gamma): integrate into trunk",
        ])
        self.assertEqual(self._head("HEAD^"), advanced)


class TestSkipCleanedFolder(AcceptAllRepositoryCase):
    """Reproduces the crashresume01 incident: a finished folder that was

    already accepted and then cleaned (branch and worktree both gone, only
    a stale receipt left behind) must not stop the ``--all`` chain.
    """

    def _accept_and_clean(self, folder: str) -> None:
        code, output = self._accept_all()
        self.assertEqual(code, 0, output)
        clean_code = clean_folder(self._config(folder))
        self.assertEqual(clean_code, 0)
        self.assertIsNone(gitops.folder_worktree(self.root, folder))
        self.assertEqual(gitops.folder_branches(self.root, folder), [])

    def test_cleaned_folder_is_skipped_and_chain_continues(self) -> None:
        self._write_task("cleaned")
        self._write_task("zzz-after")
        self._make_source("cleaned")
        self._make_source("zzz-after")
        self._accept_and_clean("cleaned")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn(
            "skip cleaned (no source branch remains; "
            "already integrated and cleaned)", output)
        self.assertNotIn("failed:    cleaned", output)
        self.assertIn("accepted:  zzz-after", output)
        self.assertNotIn("remaining: zzz-after", output)

    def test_folders_after_the_skipped_one_are_processed_normally(self) -> None:
        self._write_task("cleaned")
        self._write_task("zzz-fresh")
        self._make_source("cleaned")
        self._accept_and_clean("cleaned")
        self._write_task("zzz-fresh", status="DONE")
        self._make_source("zzz-fresh")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("skip cleaned", output)
        self.assertIn("accepted:  zzz-fresh", output)
        self.assertIn("remaining: (none)", output)

    def test_direct_accept_of_cleaned_folder_still_fails_closed(self) -> None:
        self._write_task("cleaned")
        self._make_source("cleaned")
        self._accept_and_clean("cleaned")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = accept_folder(self._config("cleaned"))

        self.assertEqual(code, 1)
        self.assertIn("no source worktree", output.getvalue())


if __name__ == "__main__":
    unittest.main()
