"""Public facade over the verification modules.

The receipts written by the modules below are a derived runtime cache.  They
record facts about explicit source snapshots, the resulting integration trees,
and the main-tree verification script; they are not human approval and never
advance a Git ref.

Two independent receipt models live side by side and never read each other's
files:

* the per-plan receipt (``<plan>/_verification.toml``), covering one plan
  merged into the target -- ``assent.plan_verification``;
* the batch receipt (``.assent/_batch_verification.toml``), covering one full
  verification of several plans merged in a recorded order, so that a batch
  release can publish them one by one against a single test run --
  ``assent.batch_receipt`` for the evidence, ``assent.batch_verification`` for
  the command that produces it.

``assent.verification_common`` holds what more than one of them needs.  This
module adds no behavior of its own: it exists so that ``accept``, ``engine``,
``reconcile``, ``reject``, ``rework``, and the CLI keep one import name for the
whole verification surface.  The per-plan entry points own their shared
closeout handoff through ``assent.plan_verification_closeout``; batch and
focused verification remain receipt paths of their own and do not refresh a
plan report.
"""
from __future__ import annotations

from assent.batch_receipt import (BATCH_RECEIPT_NAME, BATCH_RECEIPT_VERSION,
                                  BatchSource, BatchVerificationReceipt,
                                  batch_receipt_is_current, batch_receipt_path,
                                  batch_receipt_staleness,
                                  current_batch_ignored_directory_inputs,
                                  invalidate_batch_receipt, read_batch_receipt,
                                  write_batch_receipt)
from assent.batch_verification import (BatchBisectResult, BatchConflict,
                                       BatchSelection, FilteredBatchChain,
                                       bisect_batch_failure,
                                       confirm_on_terminal, select_batch_plans,
                                       select_explicit_batch_plans,
                                       verify_batch, verify_batch_selected,
                                       verify_selected_batch)
from assent.plan_verification_closeout import (verify_plan,
                                                 verify_plan_if_needed)
from assent.plan_verification import (RECEIPT_NAME, RECEIPT_VERSION,
                                        VerificationReceipt,
                                        current_ignored_directory_inputs,
                                        invalidate_plan_receipt, read_receipt,
                                        read_verification_receipt,
                                        receipt_matches_current_candidate,
                                        receipt_path, receipt_report_lines,
                                        write_receipt,
                                        write_verification_receipt)
from assent.verification_common import (
                                        VERIFY_COMMAND, BatchCandidate,
                                        build_batch_candidate,
                                        diagnosed_ignored_dirs,
                                        mentioned_ordinary_ignored_dirs,
                                        run_full_verifier, verifier_digest)

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
    "current_batch_ignored_directory_inputs",
    "bisect_batch_failure",
    "build_batch_candidate",
    "confirm_on_terminal",
    "current_ignored_directory_inputs",
    "invalidate_batch_receipt",
    "invalidate_plan_receipt",
    "read_batch_receipt",
    "read_receipt",
    "read_verification_receipt",
    "receipt_matches_current_candidate",
    "diagnosed_ignored_dirs",
    "mentioned_ordinary_ignored_dirs",
    "receipt_path",
    "receipt_report_lines",
    "run_full_verifier",
    "select_batch_plans",
    "select_explicit_batch_plans",
    "verifier_digest",
    "verify_batch",
    "verify_batch_selected",
    "verify_plan",
    "verify_plan_if_needed",
    "verify_selected_batch",
    "write_batch_receipt",
    "write_receipt",
    "write_verification_receipt",
]
