"""Compatibility facade over the verification modules.

The receipts written by the modules below are a derived runtime cache.  They
record facts about explicit source snapshots, the resulting integration trees,
and the main-tree verification script; they are not human approval and never
advance a Git ref.

Two independent receipt models live side by side and never read each other's
files:

* the per-folder receipt (``<folder>/_verification.toml``), covering one folder
  merged into the target -- ``assent.folder_verification``;
* the batch receipt (``.assent/_batch_verification.toml``), covering one full
  verification of several folders merged in a recorded order, so that a batch
  release can publish them one by one against a single test run --
  ``assent.batch_receipt`` for the evidence, ``assent.batch_verification`` for
  the command that produces it.

``assent.verification_common`` holds what more than one of them needs.  This
module adds no behavior of its own: it exists so that ``accept``, ``engine``,
``reconcile``, ``reject``, ``rework``, and the CLI keep one import name for the
whole verification surface.
"""
from __future__ import annotations

from assent.batch_receipt import (BATCH_RECEIPT_NAME, BATCH_RECEIPT_VERSION,
                                  BatchSource, BatchVerificationReceipt,
                                  batch_receipt_is_current, batch_receipt_path,
                                  batch_receipt_staleness,
                                  invalidate_batch_receipt, read_batch_receipt,
                                  write_batch_receipt)
from assent.batch_verification import (BatchBisectResult, BatchConflict,
                                       BatchSelection, FilteredBatchChain,
                                       bisect_batch_failure,
                                       confirm_on_terminal, select_batch_folders,
                                       select_explicit_batch_folders,
                                       verify_batch, verify_batch_selected,
                                       verify_selected_batch)
from assent.folder_verification import (RECEIPT_NAME, RECEIPT_VERSION,
                                        VerificationReceipt,
                                        current_shared_inputs,
                                        invalidate_folder_receipt, read_receipt,
                                        read_verification_receipt,
                                        receipt_matches_current_candidate,
                                        receipt_path, receipt_report_lines,
                                        verify_folder, verify_folder_if_needed,
                                        write_receipt,
                                        write_verification_receipt)
from assent.verification_common import (VERIFY_COMMAND, BatchCandidate,
                                        build_batch_candidate, run_full_verifier,
                                        verifier_digest)

# The full verifier used to be private to this module, and callers outside the
# verification modules still name it that way when they assert that a command
# never starts it.  Keeping the old name bound here costs nothing and saves
# every such caller from depending on which module now owns the runner.
_run_full_verifier = run_full_verifier

__all__ = [
    "BATCH_RECEIPT_NAME",
    "BATCH_RECEIPT_VERSION",
    "BatchBisectResult",
    "BatchCandidate",
    "BatchConflict",
    "BatchSelection",
    "BatchSource",
    "BatchVerificationReceipt",
    "FilteredBatchChain",
    "RECEIPT_NAME",
    "RECEIPT_VERSION",
    "VERIFY_COMMAND",
    "VerificationReceipt",
    "batch_receipt_is_current",
    "batch_receipt_path",
    "batch_receipt_staleness",
    "bisect_batch_failure",
    "build_batch_candidate",
    "confirm_on_terminal",
    "current_shared_inputs",
    "invalidate_batch_receipt",
    "invalidate_folder_receipt",
    "read_batch_receipt",
    "read_receipt",
    "read_verification_receipt",
    "receipt_matches_current_candidate",
    "receipt_path",
    "receipt_report_lines",
    "run_full_verifier",
    "select_batch_folders",
    "select_explicit_batch_folders",
    "verifier_digest",
    "verify_batch",
    "verify_batch_selected",
    "verify_folder",
    "verify_folder_if_needed",
    "verify_selected_batch",
    "write_batch_receipt",
    "write_receipt",
    "write_verification_receipt",
]
