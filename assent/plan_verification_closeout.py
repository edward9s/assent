"""The report closeout for one plan-level verification operation.

Plan verification owns receipt evidence, while inspection owns report
rendering.  This small boundary keeps those modules acyclic and gives every
plan-verification outcome the same best-effort report handoff.
"""
from __future__ import annotations

from assent.plan_verification import (verify_plan_receipt,
                                        verify_plan_receipt_if_needed,
                                        verify_plan_action as
                                        _verify_plan_action)
from assent.inspection import try_write_report as _try_write_report


def try_write_report(cfg) -> None:
    """Call the inspection layer's best-effort report writer."""
    _try_write_report(cfg)


def refresh_report(cfg) -> None:
    """Best-effort refresh for a settled plan-verification operation.

    Ordinary report failures must not alter a verification result.  The
    existing writer deliberately lets KeyboardInterrupt and SystemExit pass
    through, so this boundary preserves those control-flow signals too.
    """
    try:
        try_write_report(cfg)
    except Exception:
        pass


def verify_plan(cfg) -> int:
    """Run one plan verification, then refresh its report after lock release."""
    try:
        return verify_plan_receipt(cfg)
    finally:
        refresh_report(cfg)


def verify_plan_if_needed(cfg) -> int:
    """Run conditional plan verification with the same shared closeout."""
    try:
        return verify_plan_receipt_if_needed(cfg)
    finally:
        refresh_report(cfg)


def verify_plan_action(cfg, *, recheck=False):
    """Return typed selection evidence, then refresh after releasing locks."""
    try:
        return _verify_plan_action(cfg, recheck=recheck)
    finally:
        refresh_report(cfg)
