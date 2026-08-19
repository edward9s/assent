"""Scheduler handoff tests for unattended plan verification.

These cover who refreshes a plan receipt and when, the lock order the
scheduler entry point takes, and the shared full-verifier subprocess itself.
``assent verify --batch`` has its own module, ``tests.test_batch_verification``.

The cases that drive ``engine.run`` share ``VerificationEngineCase``, which mixes in
``GlobalContractsMixin`` so a run is gated by a temporary user home holding the current
contracts rather than by whatever the operator has in ``~/.assent``.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from assent import engine
from assent.config import load_config
from assent.plan_verification import VerificationReceipt, _verify_locked
from assent.plan_verification_closeout import (verify_plan,
                                                 verify_plan_if_needed)
from assent.verification_common import run_full_verifier
from tests.test_contracts import GlobalContractsMixin


def _task(status: str) -> str:
    return (
        'title = "Task"\n'
        'deps = []\n'
        'model = "lite"\n'
        f'status = "{status}"\n'
        'scope = ["src/"]\n'
        'verify = "python -m unittest tests.test_main"\n'
        'goal = "Finish"\n'
        'acceptance = "Focused tests pass"\n'
    )


class VerificationEngineCase(GlobalContractsMixin, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="assent verification engine "))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / "work"
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        self.task_path = self.tasks_dir / "t001_task.e.toml"
        self.cfg = load_config(self.config_path, "work")

    def write_status(self, status: str) -> None:
        self.task_path.write_text(_task(status), encoding="utf-8")

class TestPlanVerificationReportCloseout(VerificationEngineCase):
    """Every per-plan verification result refreshes its report once, outside locks."""

    def _locks(self, events: list[str]):
        @contextmanager
        def integration_lock(_path):
            events.append("integration-enter")
            try:
                yield
            finally:
                events.append("integration-exit")

        @contextmanager
        def plan_lock(*_args):
            events.append("plan-enter")
            try:
                yield
            finally:
                events.append("plan-exit")

        return integration_lock, plan_lock

    def test_success_and_failure_refresh_after_receipt_operation_and_locks(self):
        for status, expected_result in (("PASSED", 0), ("FAILED", 1)):
            with self.subTest(status=status):
                self.write_status("DONE")
                events: list[str] = []
                integration_lock, plan_lock = self._locks(events)
                receipt = SimpleNamespace(
                    status=status, source_tip="source", target_tip="target",
                    integration_tree="tree", verify_script_sha256="script",
                    shared_inputs_sha256="shared", exit_code=(
                        0 if status == "PASSED" else 1), failure_summary="")

                def verify(_cfg, *, record_conflict_receipt):
                    self.assertTrue(record_conflict_receipt)
                    events.append("receipt")
                    return receipt

                with mock.patch("assent.plan_verification.hold_integration_lock",
                                integration_lock), \
                        mock.patch("assent.plan_verification.hold_lock",
                                   plan_lock), \
                        mock.patch("assent.plan_verification._verify_locked",
                                   side_effect=verify), \
                        mock.patch(
                            "assent.plan_verification_closeout.try_write_report",
                            side_effect=lambda _cfg: events.append("report")) as report:
                    with contextlib.redirect_stdout(io.StringIO()):
                        result = verify_plan(self.cfg)

                self.assertEqual(result, expected_result)
                self.assertEqual(events, [
                    "integration-enter", "plan-enter", "receipt",
                    "plan-exit", "integration-exit", "report",
                ])
                report.assert_called_once_with(self.cfg)

    def test_refusal_and_interrupt_still_refresh_once_after_locks(self):
        for outcome in (engine.AssentError("refused"), KeyboardInterrupt()):
            with self.subTest(outcome=type(outcome).__name__):
                self.write_status("DONE")
                events: list[str] = []
                integration_lock, plan_lock = self._locks(events)

                def verify(_cfg, *, record_conflict_receipt):
                    self.assertTrue(record_conflict_receipt)
                    events.append("receipt")
                    raise outcome

                with mock.patch("assent.plan_verification.hold_integration_lock",
                                integration_lock), \
                        mock.patch("assent.plan_verification.hold_lock",
                                   plan_lock), \
                        mock.patch("assent.plan_verification._verify_locked",
                                   side_effect=verify), \
                        mock.patch(
                            "assent.plan_verification_closeout.try_write_report",
                            side_effect=lambda _cfg: events.append("report")) as report:
                    with contextlib.redirect_stdout(io.StringIO()):
                        if isinstance(outcome, KeyboardInterrupt):
                            with self.assertRaises(KeyboardInterrupt):
                                verify_plan(self.cfg)
                        else:
                            self.assertEqual(verify_plan(self.cfg), 1)

                self.assertEqual(events, [
                    "integration-enter", "plan-enter", "receipt",
                    "plan-exit", "integration-exit", "report",
                ])
                report.assert_called_once_with(self.cfg)

    def test_report_failure_cannot_change_result_or_mask_interrupt(self):
        self.write_status("DONE")
        receipt = SimpleNamespace(
            status="PASSED", source_tip="source", target_tip="target",
            integration_tree="tree", verify_script_sha256="script",
            shared_inputs_sha256="shared", exit_code=0, failure_summary="")
        with mock.patch("assent.plan_verification._verify_locked",
                        return_value=receipt) as full, \
                mock.patch(
                    "assent.plan_verification_closeout.try_write_report",
                    side_effect=OSError("report unavailable")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(verify_plan(self.cfg), 0)
        full.assert_called_once_with(self.cfg, record_conflict_receipt=True)

        with mock.patch("assent.plan_verification._verify_locked",
                        side_effect=KeyboardInterrupt) as full, \
                mock.patch(
                    "assent.plan_verification_closeout.try_write_report",
                    side_effect=OSError("report unavailable")):
            with contextlib.redirect_stdout(io.StringIO()), \
                    self.assertRaises(KeyboardInterrupt):
                verify_plan(self.cfg)
        full.assert_called_once_with(self.cfg, record_conflict_receipt=True)

    def test_incomplete_plan_and_fresh_reuse_refresh_once(self):
        events: list[str] = []
        integration_lock, plan_lock = self._locks(events)
        with mock.patch("assent.plan_verification.hold_integration_lock",
                        integration_lock), \
                mock.patch("assent.plan_verification.hold_lock", plan_lock), \
                mock.patch(
                    "assent.plan_verification_closeout.try_write_report",
                    side_effect=lambda _cfg: events.append("report")) as report:
            self.write_status("TODO")
            self.assertEqual(verify_plan_if_needed(self.cfg), 0)
            self.write_status("DONE")
            (self.tasks_dir / "_verification.toml").write_text(
                "receipt\n", encoding="utf-8")
            with mock.patch(
                    "assent.plan_verification._receipt_matches_current_candidate_locked",
                    return_value=True), \
                    mock.patch("assent.plan_verification.read_receipt",
                               return_value=SimpleNamespace(
                                   status="PASSED", integration_tree="tree")), \
                    mock.patch("assent.plan_verification.gitops.main_worktree",
                               return_value=self.root):
                self.assertEqual(verify_plan_if_needed(self.cfg), 0)
            with mock.patch(
                    "assent.plan_verification._receipt_matches_current_candidate_locked",
                    side_effect=engine.AssentError("malformed receipt")):
                self.assertEqual(verify_plan_if_needed(self.cfg), 1)

        self.assertEqual(events, [
            "integration-enter", "plan-enter", "plan-exit",
            "integration-exit", "report",
            "integration-enter", "plan-enter", "plan-exit",
            "integration-exit", "report",
            "integration-enter", "plan-enter", "plan-exit",
            "integration-exit", "report",
        ])
        self.assertEqual(report.call_count, 3)


class TestConditionalPlanVerification(VerificationEngineCase):
    def test_lock_order_is_integration_then_plan_and_fresh_pass_skips_suite(self):
        self.write_status("DONE")
        events: list[str] = []

        @contextmanager
        def integration_lock(_path):
            events.append("integration-enter")
            yield
            events.append("integration-exit")

        @contextmanager
        def plan_lock(*_args):
            events.append("plan-enter")
            yield
            events.append("plan-exit")

        receipt = VerificationReceipt(
            version=1, status="PASSED", source_tip="a" * 40,
            target_tip="b" * 40, integration_tree="c" * 40,
            verify_script_sha256="d" * 64,
            verify_command="python .assent/verify.py", exit_code=0,
            completed_at="2026-07-22T00:00:00+00:00", failure_summary="")
        (self.tasks_dir / "_verification.toml").write_text(
            "placeholder\n", encoding="utf-8")

        with mock.patch("assent.plan_verification.hold_integration_lock",
                        integration_lock), \
                mock.patch("assent.plan_verification.hold_lock", plan_lock), \
                mock.patch(
                    "assent.plan_verification._receipt_matches_current_candidate_locked",
                    return_value=True), \
                mock.patch("assent.plan_verification.read_receipt", return_value=receipt), \
                mock.patch("assent.plan_verification.gitops.main_worktree",
                           return_value=self.root), \
                mock.patch("assent.plan_verification._verify_locked") as full:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify_plan_if_needed(self.cfg)

        self.assertEqual(result, 0)
        self.assertEqual(events, [
            "integration-enter", "plan-enter", "plan-exit", "integration-exit"])
        full.assert_not_called()

    def test_invalid_existing_receipt_fails_closed_without_full_suite(self):
        self.write_status("DONE")
        (self.tasks_dir / "_verification.toml").write_text(
            "not valid = [", encoding="utf-8")
        with mock.patch(
                "assent.plan_verification._receipt_matches_current_candidate_locked",
                side_effect=engine.AssentError("bad receipt")), \
                mock.patch("assent.plan_verification._verify_locked") as full:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify_plan_if_needed(self.cfg)
        self.assertEqual(result, 1)
        full.assert_not_called()

    def test_explicit_refresh_preserves_invalid_receipt_and_starts_no_candidate(self):
        self.write_status("DONE")
        path = self.tasks_dir / "_verification.toml"
        invalid = "not valid = ["
        path.write_text(invalid, encoding="utf-8")

        with mock.patch(
                "assent.plan_verification.gitops.temporary_integration_worktree") as candidate:
            with self.assertRaises(engine.AssentError):
                _verify_locked(self.cfg)

        self.assertEqual(path.read_text(encoding="utf-8"), invalid)
        candidate.assert_not_called()


class TestVerificationPrompt(VerificationEngineCase):
    def test_timeout_is_not_defined_as_sufficient_for_blocked(self):
        self.write_status("TODO")
        task = engine.Plan.parse(self.tasks_dir).tasks[0]
        session = engine.SessionIdentity(
            agent="codex", requested_model="model", effort="heavy",
            requested_effort="high")
        prompt = engine._build_prompt(self.cfg, task, None, session)
        self.assertIn("focused task gate", prompt)
        self.assertIn("do not start a concurrent duplicate", prompt)
        self.assertIn("do not mark the task BLOCKED solely", prompt)
        self.assertIn("scheduler runs the\nsame command", prompt)


class TestFullVerifierProcess(unittest.TestCase):
    def test_slow_verifier_has_no_timeout_and_reports_elapsed_and_exit_code(self):
        completed = subprocess.CompletedProcess(["verifier"], 7, "out", "err")
        output = io.StringIO()
        with mock.patch("assent.verification_common.subprocess.run",
                        return_value=completed) as run_child, \
                mock.patch("assent.verification_common.time.monotonic",
                           side_effect=[10.0, 311.25]), \
                contextlib.redirect_stdout(output):
            actual = run_full_verifier(
                Path("verify.py"), Path("candidate with spaces"))

        self.assertIs(actual, completed)
        self.assertNotIn("timeout", run_child.call_args.kwargs)
        self.assertEqual(run_child.call_args.kwargs["cwd"],
                         "candidate with spaces")
        self.assertIn("Full verification started", output.getvalue())
        self.assertIn("elapsed 301.2s, exit code 7", output.getvalue())

    def test_invalid_utf8_output_is_escaped_and_nonzero_is_preserved(self):
        completed = subprocess.CompletedProcess(
            ["verifier"], 7, "valid output 診斷".encode("utf-8") + b"\x80",
            b"native stderr\xff")
        with mock.patch("assent.verification_common.subprocess.run",
                        return_value=completed), \
                mock.patch("assent.verification_common.time.monotonic",
                           side_effect=[10.0, 11.0]), \
                contextlib.redirect_stdout(io.StringIO()):
            actual = run_full_verifier(Path("verify.py"), Path("candidate"))

        self.assertEqual(actual.returncode, 7)
        self.assertIn("valid output 診斷", actual.stdout)
        self.assertIn(r"\x80", actual.stdout)
        self.assertIn(r"\xff", actual.stderr)
        self.assertIn("not valid UTF-8", actual.stderr)
        self.assertNotIn("\ufffd", actual.stdout + actual.stderr)

    def test_zero_exit_with_invalid_utf8_output_becomes_failure(self):
        completed = subprocess.CompletedProcess(["verifier"], 0, b"\x80", b"")
        with mock.patch("assent.verification_common.subprocess.run",
                        return_value=completed), \
                mock.patch("assent.verification_common.time.monotonic",
                           side_effect=[10.0, 11.0]), \
                contextlib.redirect_stdout(io.StringIO()):
            actual = run_full_verifier(Path("verify.py"), Path("candidate"))

        self.assertEqual(actual.returncode, 1)
        self.assertIn(r"\x80", actual.stdout)
        self.assertIn("not valid UTF-8", actual.stderr)
        self.assertNotIn("\ufffd", actual.stdout + actual.stderr)

    def test_interrupt_is_reported_and_propagated_for_candidate_cleanup(self):
        output = io.StringIO()
        with mock.patch("assent.verification_common.subprocess.run",
                        side_effect=KeyboardInterrupt), \
                mock.patch("assent.verification_common.time.monotonic",
                           side_effect=[20.0, 325.0]), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(KeyboardInterrupt):
            run_full_verifier(Path("verify.py"), Path("candidate"))

        self.assertIn("elapsed 305.0s, exit code 130", output.getvalue())


if __name__ == "__main__":
    unittest.main()
