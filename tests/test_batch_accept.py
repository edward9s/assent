"""Behavioral tests for the batch acceptance paths in ``assent.batch_accept``.

They cover how ``accept --all`` chooses between releasing a fresh batch receipt
and falling back to the per-folder path, what the explicit selected
``accept A B`` release requires, what the published merges look like, which
gates refuse a release, and which commands invalidate a batch receipt.

The sequential ``accept --all`` chain and the direct ``accept_folder``
transaction are tested by tests/test_accept_all.py and tests/test_accept.py;
this module reuses that module's repository fixture rather than rebuilding it.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from assent import gitops, verification
from assent.lockfile import hold_lock
from assent.reject import reject_folder
from assent.rework import rework_task
# A batch release publishes the same folders the ``--all`` chain publishes one
# at a time, so both exercise the same disposable repository fixture.
from tests.test_accept_all import AcceptAllRepositoryCase


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class BatchReleaseCase(AcceptAllRepositoryCase):
    """An ``accept --all`` repository plus helpers for one verified batch."""

    def _verify_batch(self, confirm: Callable[[str], bool] | None = None
                      ) -> tuple[int, str]:
        """Verify the queued batch; ``confirm`` answers a conflict-skip question.

        Left unset, ``verify_batch`` keeps its terminal default, which a batch
        that merges cleanly never reaches.
        """
        options = {} if confirm is None else {"confirm": confirm}
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verification.verify_batch(
                str(self.config_path), self.assent_dir, **options)
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


class SelectedBatchReleaseCase(BatchReleaseCase):
    """Helpers for the explicit ``accept FOLDER_A FOLDER_B`` path."""

    def _verify_selected(self, *folders: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verification.verify_selected_batch(
                str(self.config_path), self.assent_dir, folders)
        return code, output.getvalue()

    def _prepare_selected(self, *folders: str
                          ) -> verification.BatchVerificationReceipt:
        for folder in folders:
            self._write_task(folder)
            self._make_source(folder)
        code, output = self._verify_selected(*folders)
        self.assertEqual(code, 0, output)
        return self._batch_receipt()


class TestSelectedBatchRelease(SelectedBatchReleaseCase):
    def test_selected_release_normalizes_order_and_publishes_atomically(self) -> None:
        receipt = self._prepare_selected("alpha", "beta")
        target_before = self._head()

        code, output = self._accept_selected("beta", "alpha")

        self.assertEqual(code, 0, output)
        self.assertIn("accept alpha beta: batch release done", output)
        self.assertNotIn("accept --all", output)
        self.assertIn("accepted:  alpha, beta", output)
        self.assertFalse(self._batch_path().exists())
        self.assertEqual(self._accept_subjects(), [
            "accept(alpha): integrate into trunk",
            "accept(beta): integrate into trunk",
        ])
        self.assertEqual(self._head("HEAD^1^1"), target_before)
        self.assertEqual(self._head("HEAD^1^2"), receipt.sources[0].source_tip)
        self.assertEqual(self._head("HEAD^2"), receipt.sources[1].source_tip)

    def test_selected_release_requires_the_exact_receipt_set(self) -> None:
        self._prepare_selected("alpha", "beta")
        self._write_task("gamma")
        self._make_source("gamma")
        target_before = self._head()

        code, output = self._accept_selected("gamma", "alpha")

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertTrue(self._batch_path().exists())
        self.assertIn("not the exact selected set", output)
        self.assertIn("assent verify alpha gamma", output)
        self.assertNotIn("accept --all", output)

    def test_selected_release_refuses_stale_receipt_without_verification_or_fallback(
            self) -> None:
        self._prepare_selected("alpha", "beta")
        source = gitops.folder_worktree(self.root, "alpha")
        self.assertIsNotNone(source)
        assert source is not None
        (source / "changed.txt").write_text("drift\n", encoding="utf-8")
        gitops.commit_all(source, "drift alpha")
        target_before = self._head()

        with patch("assent.batch_accept.verification.verify_folder_if_needed",
                   side_effect=AssertionError("selected accept verified")):
            code, output = self._accept_selected("alpha", "beta")

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertTrue(self._batch_path().exists())
        self.assertIn("not fresh", output)
        self.assertIn("assent verify alpha beta", output)

    def test_selected_release_refuses_failed_receipt_and_keeps_it(self) -> None:
        receipt = self._prepare_selected("alpha", "beta")
        verification.write_batch_receipt(
            self._batch_path(), replace(receipt, status="FAILED", exit_code=1,
                                         failure_summary="failed"), self.root)
        target_before = self._head()

        code, output = self._accept_selected("alpha", "beta")

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertTrue(self._batch_path().exists())
        self.assertIn("not fresh", output)
        self.assertIn("assent verify alpha beta", output)


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


class TestPartialBatchRelease(BatchReleaseCase):
    """Release a batch that conflict filtering shrank to part of the queue.

    ``verify --batch`` leaves out a folder whose source conflicts together with
    everything queued after it, so the receipt it writes can skip a folder in
    the middle of the publishing order.  The release must reproduce that exact
    chain, leave the omitted work untouched and recoverable, and say what it did
    not publish.
    """

    def _prepare_partial_batch(self) -> verification.BatchVerificationReceipt:
        """Verify a batch that keeps alpha and delta but omits beta and gamma.

        ``beta`` collides with the ``shared.txt`` ``alpha`` already merged, and
        ``gamma`` is queued after ``beta``, so the receipt covers a subset that
        is not a prefix of the queue: it ends with ``delta``, which the omitted
        ``beta`` precedes.
        """
        for folder in ("alpha", "beta", "delta", "gamma"):
            self._write_task(folder)
        self._write_after("gamma", ("beta",))
        self._make_source("alpha", filename="shared.txt", content="alpha\n")
        self._make_source("beta", filename="shared.txt", content="beta\n")
        self._make_source("delta")
        self._make_source("gamma")

        code, output = self._verify_batch(confirm=lambda question: True)

        self.assertEqual(code, 0, output)
        receipt = self._batch_receipt()
        self.assertEqual(receipt.folders, ("alpha", "delta"))
        return receipt

    def _assert_recoverable(self, folder: str) -> None:
        """The omitted folder still has exactly the source a rework would need."""
        branches = gitops.folder_branches(self.root, folder)
        self.assertEqual(len(branches), 1, folder)
        self.assertIsNotNone(gitops.folder_worktree(self.root, folder))
        self.assertFalse(gitops.is_ancestor(
            self.root, gitops.branch_tip(self.root, branches[0]), self._head()))

    def test_a_partial_batch_publishes_its_subset_and_nothing_else(self) -> None:
        receipt = self._prepare_partial_batch()
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("batch receipt covers: alpha, delta", output)
        self.assertEqual(self._accept_subjects(), [
            "accept(alpha): integrate into trunk",
            "accept(delta): integrate into trunk",
        ])
        # Exactly the recorded step trees, chained onto the untouched target.
        self.assertEqual(self._head("HEAD^1^1"), target_before)
        self.assertEqual(self._head("HEAD^1^2"), receipt.sources[0].source_tip)
        self.assertEqual(self._head("HEAD^2"), receipt.sources[1].source_tip)
        self.assertEqual(gitops.tree_of(self.root, "HEAD^1"),
                         receipt.sources[0].step_tree)
        self.assertEqual(gitops.tree_of(self.root, "HEAD"), receipt.final_tree)
        # The conflicting folder's content never reached the target, and both
        # it and its downstream can still be reworked or rejected.
        self.assertEqual((self.root / "shared.txt").read_text(encoding="utf-8"),
                         "alpha\n")
        self.assertFalse((self.root / "gamma.txt").exists())
        self._assert_recoverable("beta")
        self._assert_recoverable("gamma")

    def test_the_release_names_the_omitted_folders_neutrally(self) -> None:
        self._prepare_partial_batch()

        code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertIn("2 finished folder(s) still not accepted: beta, gamma",
                      output)
        # beta and gamma were finished before the batch was verified, so the
        # wording must not present being outside the batch as finishing late.
        self.assertNotIn("finished after it was verified", output)

    def test_a_partial_release_neither_verifies_nor_asks_anything(self) -> None:
        counter = self.parent / "verify_runs.log"
        counter.write_text("", encoding="utf-8")
        self._write_verify(
            "import pathlib\n"
            f"pathlib.Path({str(counter)!r}).open('a', encoding='utf-8').write('run\\n')\n"
            "raise SystemExit(0)\n")
        self._prepare_partial_batch()
        during_verify = counter.read_text(encoding="utf-8").count("run\n")

        with patch("builtins.input",
                   side_effect=AssertionError("the release asked a question")):
            code, output = self._accept_all()

        self.assertEqual(code, 0, output)
        self.assertEqual(counter.read_text(encoding="utf-8").count("run\n"),
                         during_verify)
        self.assertNotIn("per-folder verify+accept", output)

    def test_the_receipt_records_no_skip_metadata(self) -> None:
        self._prepare_partial_batch()

        text = self._batch_path().read_text(encoding="utf-8")

        self.assertIn("alpha", text)
        for omitted in ("beta", "gamma", "skip"):
            self.assertNotIn(omitted, text)

    def test_replay_refuses_a_source_whose_prerequisite_was_omitted(self) -> None:
        """A receipt is evidence, not authorization.

        Declaring ``delta`` after the omitted, still-unaccepted ``beta`` makes
        the recorded chain one that would carry no upstream while claiming the
        order held, so the prerequisite gate must refuse the whole release.
        """
        self._prepare_partial_batch()
        self._write_after("delta", ("beta",))
        target_before = self._head()

        code, output = self._accept_all()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), target_before)
        self.assertIn("prerequisite beta of delta", output)
        self.assertEqual(self._accept_subjects(), [])
        self.assertTrue(self._batch_path().exists())


if __name__ == "__main__":
    unittest.main()
