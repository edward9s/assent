"""Behavioral tests for ``assent accept --all``: finished-folder selection,
dependency order, verify-then-accept interleaving, fail-closed chain stop,
and idempotent rerun.

The second half of this module covers the batch release path: how ``--all``
chooses between releasing a fresh batch receipt and the per-folder path, what
the released merges look like, which gates refuse a release, and which commands
invalidate a batch receipt.

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
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from assent import gitops, verification
from assent.accept import accept_all, accept_folder
from assent.clean import clean_folder
from assent.config import load_config
from assent.lockfile import hold_lock
from assent.reject import reject_folder
from assent.rework import rework_task

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


class BatchReleaseCase(AcceptAllRepositoryCase):
    """An ``accept --all`` repository plus helpers for one verified batch."""

    def _verify_batch(self) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verification.verify_batch(str(self.config_path), self.assent_dir)
        return code, output.getvalue()

    def _batch_path(self) -> Path:
        return verification.batch_receipt_path(self.assent_dir)

    def _batch_receipt(self) -> verification.BatchVerificationReceipt:
        return verification.read_batch_receipt(self._batch_path(), self.root)

    def _prepare_batch(self, *folders: str
                       ) -> verification.BatchVerificationReceipt:
        """Finish every folder, then certify them all with one full verification."""
        for folder in folders:
            self._write_task(folder)
            self._make_source(folder)
        code, output = self._verify_batch()
        self.assertEqual(code, 0, output)
        return self._batch_receipt()

    def _message(self, ref: str) -> str:
        return _git(self.root, "log", "-1", "--format=%B", ref)


class TestBatchReleasePublication(BatchReleaseCase):
    def test_batch_release_publishes_every_folder_in_one_ref_update(self) -> None:
        receipt = self._prepare_batch("alpha", "beta")
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("batch release", output)
        self.assertIn("all or nothing", output)
        self.assertIn("batch receipt covers: alpha, beta", output)
        self.assertEqual(self._accept_subjects(), [
            "accept(alpha): integrate into trunk",
            "accept(beta): integrate into trunk",
        ])
        # Two independent no-ff merges chained onto the untouched target: the
        # exact graph two single-folder accepts would have left behind.
        self.assertEqual(self._head("HEAD^1^1"), target_before)
        self.assertEqual(self._head("HEAD^1^2"), receipt.sources[0].source_tip)
        self.assertEqual(self._head("HEAD^2"), receipt.sources[1].source_tip)
        self.assertEqual(gitops.tree_of(self.root, "HEAD^1"),
                         receipt.sources[0].step_tree)
        self.assertEqual(gitops.tree_of(self.root, "HEAD"), receipt.final_tree)

    def test_each_published_merge_carries_single_folder_accept_evidence(self) -> None:
        receipt = self._prepare_batch("alpha", "beta")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        for ref, source in zip(("HEAD^1", "HEAD"), receipt.sources):
            branch = gitops.folder_branches(self.root, source.folder)[0]
            expected = gitops.accept_commit_message(
                f"accept({source.folder}): integrate into trunk", source.folder,
                branch, source.source_tip, source.step_tree,
                receipt.verify_script_sha256)
            self.assertEqual(self._message(ref), expected.strip())

    def test_a_batched_merge_message_matches_a_per_folder_accept_message(self) -> None:
        """Audit granularity must not change with the path that published it."""
        self._prepare_batch("alpha")
        batch_code, batch_output = self._accept_all()
        self.assertEqual(batch_code, 0, batch_output)
        batched = self._message("HEAD")

        self._write_task("zulu")
        self._make_source("zulu")
        per_folder_code, per_folder_output = self._accept_all()
        self.assertEqual(per_folder_code, 0, per_folder_output)
        per_folder = self._message("HEAD")

        def trailer_keys(message: str) -> list[str]:
            return [line.split(":")[0] for line in message.splitlines()
                    if line.startswith("Assent-")]

        self.assertEqual(trailer_keys(batched), trailer_keys(per_folder))
        self.assertEqual(batched.splitlines()[0],
                         "accept(alpha): integrate into trunk")
        self.assertEqual(per_folder.splitlines()[0],
                         "accept(zulu): integrate into trunk")
        self.assertIn("Assent-Folder: alpha", batched)
        self.assertIn("Assent-Folder: zulu", per_folder)

    def test_the_consumed_receipt_makes_a_rerun_an_idempotent_noop(self) -> None:
        self._prepare_batch("alpha", "beta")
        first_code, first_output = self._accept_all()
        self.assertEqual(first_code, 0, first_output)
        self.assertFalse(self._batch_path().exists())
        published = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertEqual(self._head(), published)
        self.assertIn("no batch verification receipt", output)
        self.assertIn("per-folder verify+accept", output)
        self.assertIn("already accepted", output)


class TestBatchPathSelection(BatchReleaseCase):
    def test_without_a_batch_receipt_the_per_folder_path_announces_itself(self) -> None:
        self._write_task("solo")
        self._make_source("solo")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("no batch verification receipt", output)
        self.assertIn("per-folder verify+accept", output)
        self.assertNotIn("batch release", output)
        self.assertEqual(self._accept_subjects(),
                         ["accept(solo): integrate into trunk"])

    def test_an_expired_batch_receipt_is_deleted_and_the_per_folder_path_runs(
            self) -> None:
        self._prepare_batch("alpha", "beta")
        # One more commit on one source expires the whole batch: the recorded
        # chain no longer describes the sources that exist now.
        worktree = gitops.folder_worktree(self.root, "alpha")
        (worktree / "extra.txt").write_text("more\n", encoding="utf-8")
        gitops.commit_all(worktree, "more alpha work")

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("batch verification receipt has expired", output)
        self.assertIn("per-folder verify+accept", output)
        self.assertFalse(self._batch_path().exists())
        self.assertEqual(self._accept_subjects(), [
            "accept(alpha): integrate into trunk",
            "accept(beta): integrate into trunk",
        ])


class TestBatchReleaseGates(BatchReleaseCase):
    def test_a_dirty_main_worktree_refuses_and_keeps_the_receipt(self) -> None:
        self._prepare_batch("alpha", "beta")
        (self.root / "README.md").write_text("edited\n", encoding="utf-8")
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("is not clean", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertTrue(self._batch_path().exists())

    def test_a_dirty_source_worktree_refuses_the_whole_batch(self) -> None:
        self._prepare_batch("alpha", "beta")
        worktree = gitops.folder_worktree(self.root, "beta")
        (worktree / "beta.txt").write_text("uncommitted\n", encoding="utf-8")
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("beta", output)
        self.assertIn("not clean", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertTrue(self._batch_path().exists())

    def test_a_busy_folder_lock_refuses_the_whole_batch(self) -> None:
        self._prepare_batch("alpha", "beta")
        target_before = self._head()

        with hold_lock(self.assent_dir / "beta", "beta"):
            code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("accept --all: refused", output)
        self.assertIn("beta", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertTrue(self._batch_path().exists())

    def test_a_reopened_batched_folder_refuses_the_release(self) -> None:
        self._prepare_batch("alpha", "beta")
        self._write_task("beta", status="WIP")
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("no longer finished", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertTrue(self._batch_path().exists())

    def test_a_batch_never_publishes_a_folder_ahead_of_its_prerequisite(self) -> None:
        """The same refusal a single-folder accept makes, widened to the batch.

        ``upstream`` is unfinished, so only ``downstream`` -- whose branch was
        cut from upstream's tip -- enters the batch. Publishing it alone would
        carry upstream's unverified commits into the target.
        """
        self._write_task("upstream", status="WIP")
        _worktree, _branch, upstream_tip = self._make_source("upstream")
        self._write_task("zdownstream")
        self._write_after("zdownstream", ("upstream",))
        self._make_source("zdownstream", start_snapshot=upstream_tip)
        verify_code, verify_output = self._verify_batch()
        self.assertEqual(verify_code, 0, verify_output)
        self.assertEqual(self._batch_receipt().folders, ("zdownstream",))
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("prerequisite upstream of zdownstream", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertTrue(self._batch_path().exists())

    def test_a_step_tree_that_does_not_replay_aborts_with_the_target_untouched(
            self) -> None:
        receipt = self._prepare_batch("alpha", "beta")
        # A receipt claiming a step tree the chain does not produce, held fresh
        # by the freshness oracle: the replay compares every step itself, so the
        # release must still refuse rather than trust the receipt.
        sources = list(receipt.sources)
        sources[0] = verification.BatchSource(
            sources[0].folder, sources[0].source_tip,
            gitops.tree_of(self.root, "HEAD"))
        verification.write_batch_receipt(
            self._batch_path(), replace(receipt, sources=tuple(sources)),
            self.root)
        target_before = self._head()

        with patch.object(verification, "batch_receipt_staleness",
                          return_value=()):
            code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("not the verified", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertIn("assent verify --batch", output)


class TestBatchReceiptInvalidation(BatchReleaseCase):
    def _write_folder_lock(self, folder: str) -> None:
        (self.assent_dir / folder / "assent.lock").write_text(
            f'folder = "{folder}"\n', encoding="utf-8")

    def test_reject_invalidates_the_batch_receipt(self) -> None:
        self._prepare_batch("alpha", "beta")
        self._write_folder_lock("beta")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = reject_folder(self._config("beta"),
                                 confirm=lambda prompt: "y")

        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("batch verification receipt invalidated", output.getvalue())
        self.assertFalse(self._batch_path().exists())

    def test_rework_invalidates_the_batch_receipt(self) -> None:
        self._prepare_batch("alpha", "beta")
        self._write_folder_lock("alpha")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = rework_task(self._config("alpha"), "t001",
                               reason="reopen the batched folder")

        self.assertEqual(code, 0, output.getvalue())
        self.assertIn("batch verification receipt invalidated", output.getvalue())
        self.assertFalse(self._batch_path().exists())

    def test_a_rejected_folder_can_no_longer_be_released_as_a_batch(self) -> None:
        self._prepare_batch("alpha", "beta")
        self._write_folder_lock("beta")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                reject_folder(self._config("beta"), confirm=lambda prompt: "y"),
                0)

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("no batch verification receipt", output)
        self.assertIn("per-folder verify+accept", output)
        self.assertEqual(self._accept_subjects(),
                         ["accept(alpha): integrate into trunk"])


if __name__ == "__main__":
    unittest.main()
