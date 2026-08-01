"""The report closeout for one folder-level verification operation.

Folder verification owns receipt evidence, while inspection owns report
rendering.  This small boundary keeps those modules acyclic and gives every
folder-verification outcome the same best-effort report handoff.
"""
from __future__ import annotations

from importlib import import_module

from assent.folder_verification import (verify_folder_receipt,
                                        verify_folder_receipt_if_needed)


def try_write_report(cfg) -> None:
    """Call the existing report writer without importing inspection at load time."""
    # ``inspection`` imports the verification facade to render receipt lines.
    # Importing it lazily keeps this orchestration layer acyclic too.
    inspection = import_module("assent.inspection")
    inspection.try_write_report(cfg)


def refresh_report(cfg) -> None:
    """Best-effort refresh for a settled folder-verification operation.

    Ordinary report failures must not alter a verification result.  The
    existing writer deliberately lets KeyboardInterrupt and SystemExit pass
    through, so this boundary preserves those control-flow signals too.
    """
    try:
        try_write_report(cfg)
    except Exception:
        pass


def verify_folder(cfg) -> int:
    """Run one folder verification, then refresh its report after lock release."""
    try:
        return verify_folder_receipt(cfg)
    finally:
        refresh_report(cfg)


def verify_folder_if_needed(cfg) -> int:
    """Run conditional folder verification with the same shared closeout."""
    try:
        return verify_folder_receipt_if_needed(cfg)
    finally:
        refresh_report(cfg)
