"""Batch verification receipt schema, byte layout, and freshness tests.

The batch receipt is evidence only: these tests never run the full verifier.
They build the merge chain two finished folders currently produce, write the
receipt that describes it, and then check what survives a round trip and what
expires it.
"""
from __future__ import annotations

import dataclasses
import subprocess
import tomllib
import unittest
from pathlib import Path

from assent import AssentError
from assent.batch_receipt import (BATCH_RECEIPT_NAME, BATCH_RECEIPT_VERSION,
                                  BatchSource, BatchVerificationReceipt,
                                  batch_receipt_is_current, batch_receipt_path,
                                  batch_receipt_staleness,
                                  current_batch_shared_inputs,
                                  read_batch_receipt, write_batch_receipt)
from assent.folder_verification import RECEIPT_NAME, read_receipt
from assent.gitops import commit_of, tree_of
from assent.verification_common import build_batch_candidate, verifier_digest
from assent.verification import verify_folder
from tests.link_support import make_directory_link
# The batch receipt describes the same repository the per-folder receipt tests
# already build, so the fixture is shared rather than copied.
from tests.test_verification import VerificationRepositoryCase


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


class BatchReceiptCase(VerificationRepositoryCase):
    """Two independent folders queued behind one full verification."""

    def setUp(self) -> None:
        super().setUp()
        self.second_worktree = self.source_worktree.parent / "plan貳"
        _git(self.root, "worktree", "add", "-b", "plan貳/run",
             str(self.second_worktree), self.target_tip)
        (self.second_worktree / "second.txt").write_text(
            "second result\n", encoding="utf-8")
        _git(self.second_worktree, "add", "second.txt")
        _git(self.second_worktree, "commit", "-m", "second result")
        self.second_tip = commit_of(self.root, "plan貳/run")
        self.order = (("plan測試", self.source_tip), ("plan貳", self.second_tip))

    def _batch_receipt(self, **overrides) -> BatchVerificationReceipt:
        """Build a receipt from the merge chain the folders currently produce."""
        candidate = build_batch_candidate(self.root, self.target_tip, self.order)
        self.assertTrue(candidate.ok, candidate.conflicts)
        sources = tuple(
            BatchSource(folder, tip, tree)
            for (folder, tip), tree in zip(self.order, candidate.step_trees))
        fields = dict(
            version=BATCH_RECEIPT_VERSION, status="PASSED",
            target_tip=self.target_tip,
            sources=sources, final_tree=candidate.step_trees[-1],
            verify_script_sha256=verifier_digest(self.cfg),
            shared_inputs_sha256="0" * 64,
            verify_command="python .assent/verify.py", exit_code=0,
            completed_at="2026-07-24T00:00:00+00:00", failure_summary="")
        fields.update(overrides)
        receipt = BatchVerificationReceipt(**fields)
        if "shared_inputs_sha256" not in overrides:
            # The reviewed shared inputs are part of the evidence now, so the
            # fixture records what the repository currently is rather than a
            # placeholder that would read as drift.
            receipt = dataclasses.replace(
                receipt,
                shared_inputs_sha256=current_batch_shared_inputs(
                    self.root, receipt))
        return receipt

    def _write(self, receipt: BatchVerificationReceipt) -> Path:
        path = batch_receipt_path(self.assent_dir)
        write_batch_receipt(path, receipt, self.root)
        return path

    def _advance_trunk(self, name: str, content: str) -> None:
        (self.root / name).write_text(content, encoding="utf-8")
        _git(self.root, "add", name)
        _git(self.root, "commit", "-m", f"target {name}")

    def _drop_second_source(self) -> None:
        _git(self.root, "worktree", "remove", "--force", str(self.second_worktree))
        _git(self.root, "branch", "-D", "plan貳/run")


class TestBatchReceiptSchema(BatchReceiptCase):
    def test_round_trip_keeps_order_and_every_step_tree(self):
        receipt = self._batch_receipt()
        path = self._write(receipt)

        self.assertEqual(path, self.assent_dir / BATCH_RECEIPT_NAME)
        self.assertEqual(read_batch_receipt(path, self.root), receipt)
        self.assertEqual(receipt.folders, ("plan測試", "plan貳"))
        step_trees = [source.step_tree for source in receipt.sources]
        self.assertEqual(len(set(step_trees)), 2)
        self.assertEqual(receipt.final_tree, step_trees[-1])
        # The first step tree is the target with only the first folder merged.
        self.assertEqual(step_trees[0], tree_of(self.root, self.source_tip))
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertEqual([entry["folder"] for entry in data["sources"]],
                         ["plan測試", "plan貳"])

    def test_missing_unknown_and_inconsistent_content_fails_closed(self):
        receipt = self._batch_receipt()
        path = self._write(receipt)
        original = path.read_text(encoding="utf-8")
        first, second = receipt.sources
        sources_block = original[original.index("[[sources]]"):]
        bad_values = (
            original + 'unknown = "value"\n',
            original.replace('status = "PASSED"\n', ""),
            original.replace(f'target_tip = "{receipt.target_tip}"\n', ""),
            original.replace(sources_block, "sources = []\n"),
            original.replace(f'step_tree = "{second.step_tree}"\n', "", 1),
            original.replace(f'folder = "{second.folder}"',
                             f'folder = "{first.folder}"'),
            original.replace(f'source_tip = "{first.source_tip}"',
                             'source_tip = "abcd"'),
            original.replace(f'final_tree = "{receipt.final_tree}"',
                             f'final_tree = "{first.step_tree}"'),
            original.replace(f'step_tree = "{first.step_tree}"',
                             f'step_tree = "{first.source_tip}"'),
            original.replace(
                f'verify_script_sha256 = "{receipt.verify_script_sha256}"',
                'verify_script_sha256 = "xyz"'),
            original.replace("exit_code = 0", "exit_code = 3"),
            original.replace('completed_at = "2026-07-24T00:00:00+00:00"',
                             'completed_at = "2026-07-24T00:00:00"'),
            original.replace('folder = "plan貳"', 'folder = "../escape"'),
        )
        for text in bad_values:
            with self.subTest(text=text[-80:]):
                self.assertNotEqual(text, original)
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(AssentError):
                    read_batch_receipt(path, self.root)

    def test_empty_sources_and_wrong_types_are_rejected_before_writing(self):
        path = self.assent_dir / "unwritten.toml"
        for overrides in (
                {"sources": ()},
                {"version": True},
                {"status": "PASSING"},
                {"exit_code": True},
                {"verify_command": "python -m unittest"},
                {"failure_summary": "x" * 4001},
                {"status": "FAILED", "exit_code": 0},
        ):
            with self.subTest(**overrides):
                with self.assertRaises(AssentError):
                    write_batch_receipt(
                        path, self._batch_receipt(**overrides), self.root)
                self.assertFalse(path.exists())

    def test_a_failed_batch_receipt_keeps_its_diagnosis(self):
        receipt = self._batch_receipt(
            status="FAILED", exit_code=7,
            failure_summary="Verification command failed: exit code 7")
        path = self._write(receipt)
        self.assertEqual(read_batch_receipt(path, self.root), receipt)
        self.assertFalse(batch_receipt_is_current(self.cfg, receipt))

    def test_single_folder_receipt_is_untouched_byte_for_byte(self):
        self.assertEqual(verify_folder(self.cfg), 0)
        folder_receipt = self.tasks_dir / RECEIPT_NAME
        before = folder_receipt.read_bytes()

        receipt = self._batch_receipt()
        path = self._write(receipt)
        self.assertEqual(read_batch_receipt(path, self.root), receipt)

        self.assertEqual(folder_receipt.read_bytes(), before)
        self.assertNotIn("sources", before.decode("utf-8"))
        self.assertFalse((self.tasks_dir / BATCH_RECEIPT_NAME).exists())
        self.assertEqual(read_receipt(folder_receipt, self.root).status, "PASSED")


class TestBatchReceiptStaleness(BatchReceiptCase):
    def test_unchanged_sources_and_verifier_stay_current(self):
        receipt = self._batch_receipt()
        self.assertEqual(batch_receipt_staleness(self.cfg, receipt), ())
        self.assertTrue(batch_receipt_is_current(self.cfg, receipt))

    def test_undeclared_source_link_makes_shared_inputs_non_reproducible(self):
        receipt = self._batch_receipt()
        external = self._link_target("late batch input")
        make_directory_link(self.source_worktree / "pkg", external)

        reasons = batch_receipt_staleness(self.cfg, receipt)

        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("reviewed shared inputs cannot be reproduced", reasons[0])
        self.assertIn("outside its active REVIEWED-NONE", reasons[0])
        self.assertEqual(
            (external / "marker.txt").read_text(encoding="utf-8"),
            "late batch input marker\n")

    def test_target_metadata_may_advance_but_content_may_not(self):
        receipt = self._batch_receipt()
        _git(self.root, "commit", "--allow-empty", "-m", "metadata only")
        self.assertEqual(batch_receipt_staleness(self.cfg, receipt), ())

        self._advance_trunk("target-only.txt", "target content\n")
        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("rebuilt step tree for plan測試", reasons[0])

    def test_a_moved_source_tip_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        (self.second_worktree / "third.txt").write_text("more", encoding="utf-8")
        _git(self.second_worktree, "add", "third.txt")
        _git(self.second_worktree, "commit", "-m", "second moved")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("source tip for plan貳 changed", reasons[0])
        self.assertFalse(batch_receipt_is_current(self.cfg, receipt))

    def test_a_source_accepted_on_its_own_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        _git(self.root, "merge", "--no-ff", "-m", "accept(plan貳)", "plan貳/run")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("plan貳 has already been accepted", reasons[0])

    def test_a_vanished_source_branch_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        self._drop_second_source()

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(
            reasons, ("source branch for plan貳 no longer exists",))

    def test_an_ambiguous_source_branch_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        _git(self.root, "branch", "plan貳/second", self.second_tip)

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("source branch for plan貳 is ambiguous", reasons[0])

    def test_a_changed_verifier_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        with open(self.assent_dir / "verify.py", "a", encoding="utf-8") as handle:
            handle.write("# changed verifier content\n")

        self.assertEqual(batch_receipt_staleness(self.cfg, receipt),
                         ("verification script changed",))

    def test_every_drifted_source_is_reported_together(self):
        receipt = self._batch_receipt()
        self._drop_second_source()
        (self.source_worktree / "extra.txt").write_text("extra", encoding="utf-8")
        _git(self.source_worktree, "add", "extra.txt")
        _git(self.source_worktree, "commit", "-m", "first moved")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 2, reasons)
        self.assertIn("source tip for plan測試 changed", reasons[0])
        self.assertEqual(reasons[1], "source branch for plan貳 no longer exists")

    def test_a_target_that_now_conflicts_expires_the_whole_batch(self):
        receipt = self._batch_receipt()
        self._advance_trunk("result.txt", "target owns this file\n")

        reasons = batch_receipt_staleness(self.cfg, receipt)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertIn("rebuilt integration of plan測試 conflicts", reasons[0])
        self.assertIn("result.txt", reasons[0])

    def test_rebuilding_leaves_no_temporary_branch_or_worktree(self):
        receipt = self._batch_receipt()
        self.assertEqual(batch_receipt_staleness(self.cfg, receipt), ())
        self.assertEqual(self._temporary_resources(), ([], []))


if __name__ == "__main__":
    unittest.main()
