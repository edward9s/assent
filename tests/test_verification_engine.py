"""Scheduler handoff tests for unattended folder verification.

These cover who refreshes a folder receipt and when, the lock order the
scheduler entry point takes, and the shared full-verifier subprocess itself.
``assent verify --batch`` has its own module, ``tests.test_batch_verification``.
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
from unittest import mock

from assent import engine
from assent.config import load_config
from assent.folder_verification import (VerificationReceipt, _verify_locked,
                                        verify_folder_if_needed)
from assent.verification_common import run_full_verifier


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


class VerificationEngineCase(unittest.TestCase):
    def setUp(self) -> None:
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

    def set_receipt_refresh(self, mode: str) -> None:
        """State the [verification] receipt_refresh policy and reload the config."""
        self.config_path.write_text(
            f'[verification]\nreceipt_refresh = "{mode}"\n', encoding="utf-8")
        self.cfg = load_config(self.config_path, "work")


class TestRunVerificationHandoff(VerificationEngineCase):
    def setUp(self) -> None:
        super().setUp()
        # This class is about the handoff itself, which only run closeout under
        # the "auto" policy performs; the default "manual" has its own tests in
        # TestRunCloseoutReceiptPolicy below.
        self.set_receipt_refresh("auto")

    def _run_with_body(self, body, verify_result=0, **options):
        events: list[str] = []

        @contextmanager
        def folder_lock(*_args):
            events.append("folder-enter")
            yield
            events.append("folder-exit")

        def verify(_cfg):
            events.append("verify")
            return verify_result

        with mock.patch("assent.engine.lockfile.hold_lock", folder_lock), \
                mock.patch("assent.engine._run_locked", side_effect=body), \
                mock.patch("assent.engine.verification.verify_folder_if_needed",
                           side_effect=verify), \
                mock.patch("assent.engine.try_write_report"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = engine.run(self.cfg, **options)
        return result, events

    def test_last_task_triggers_after_session_folder_lock_is_released(self):
        self.write_status("TODO")

        def body(*_args):
            self.write_status("DONE")
            return 0

        result, events = self._run_with_body(body, once=True)
        self.assertEqual(result, 0)
        self.assertEqual(events, ["folder-enter", "folder-exit", "verify"])

    def test_once_task_and_normal_runs_verify_only_when_folder_is_complete(self):
        for options in ({}, {"once": True}, {"task_id": "t001"}):
            with self.subTest(options=options):
                self.write_status("TODO")

                def body(*_args):
                    self.write_status("DONE")
                    return 0

                result, events = self._run_with_body(body, **options)
                self.assertEqual(result, 0)
                self.assertEqual(events[-1], "verify")

        for status in ("TODO", "WIP", "BLOCKED"):
            with self.subTest(status=status):
                self.write_status(status)
                result, events = self._run_with_body(lambda *_args: 0)
                self.assertEqual(result, 0)
                self.assertNotIn("verify", events)

    def test_full_verification_failure_is_folder_level_and_nonzero(self):
        self.write_status("DONE")
        result, events = self._run_with_body(lambda *_args: 0, verify_result=1)
        self.assertEqual(result, 1)
        self.assertEqual(events.count("verify"), 1)
        self.assertIn('status = "DONE"', self.task_path.read_text(encoding="utf-8"))

    def test_task_run_failure_never_starts_full_verification(self):
        self.write_status("DONE")
        result, events = self._run_with_body(lambda *_args: 1)
        self.assertEqual(result, 1)
        self.assertNotIn("verify", events)


class TestRunCloseoutReceiptPolicy(VerificationEngineCase):
    """[verification] receipt_refresh decides who refreshes the folder receipt.

    The default "manual" makes run closeout cost zero full verifications, which is
    what a batch workflow wants; "auto" is the pre-policy behavior kept available
    for projects that want a run to end knowing the candidate is green.
    """

    def _run_closeout(self, **options) -> tuple[int, list[str], str]:
        calls: list[str] = []
        self.write_status("TODO")

        def body(*_args):
            self.write_status("DONE")
            return 0

        def verify(_cfg):
            calls.append("verify")
            print("verify work: passed (deadbeef)")
            return 0

        @contextmanager
        def folder_lock(*_args):
            yield

        out = io.StringIO()
        with mock.patch("assent.engine.lockfile.hold_lock", folder_lock), \
                mock.patch("assent.engine._run_locked", side_effect=body), \
                mock.patch("assent.engine.verification.verify_folder_if_needed",
                           side_effect=verify), \
                mock.patch("assent.engine.try_write_report") as report:
            with contextlib.redirect_stdout(out):
                result = engine.run(self.cfg, once=True, **options)
        self.assertTrue(report.called)
        return result, calls, out.getvalue()

    def test_default_manual_runs_no_full_verification_and_states_the_next_step(self):
        result, calls, output = self._run_closeout()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [])
        self.assertIn("receipt refresh deferred (default)", output)
        self.assertIn("assent verify [--batch]", output)
        self.assertFalse((self.tasks_dir / "_verification.toml").exists())

    def test_explicit_manual_matches_the_default(self):
        self.set_receipt_refresh("manual")
        result, calls, output = self._run_closeout()
        self.assertEqual(result, 0)
        self.assertEqual(calls, [])
        self.assertIn("receipt refresh deferred (default)", output)

    def test_auto_keeps_the_previous_closeout_behavior_and_output(self):
        self.set_receipt_refresh("auto")
        result, calls, output = self._run_closeout()
        self.assertEqual(result, 0)
        self.assertEqual(calls, ["verify"])
        # Nothing is added around the verification layer's own line under "auto".
        self.assertEqual(output, "verify work: passed (deadbeef)\n")


class TestAutomaticReceiptPolicy(VerificationEngineCase):
    def test_lock_order_is_integration_then_folder_and_fresh_pass_skips_suite(self):
        self.write_status("DONE")
        events: list[str] = []

        @contextmanager
        def integration_lock(_path):
            events.append("integration-enter")
            yield
            events.append("integration-exit")

        @contextmanager
        def folder_lock(*_args):
            events.append("folder-enter")
            yield
            events.append("folder-exit")

        receipt = VerificationReceipt(
            version=1, status="PASSED", source_tip="a" * 40,
            target_tip="b" * 40, integration_tree="c" * 40,
            verify_script_sha256="d" * 64,
            verify_command="python .assent/verify.py", exit_code=0,
            completed_at="2026-07-22T00:00:00+00:00", failure_summary="")
        (self.tasks_dir / "_verification.toml").write_text(
            "placeholder\n", encoding="utf-8")

        with mock.patch("assent.folder_verification.hold_integration_lock",
                        integration_lock), \
                mock.patch("assent.folder_verification.hold_lock", folder_lock), \
                mock.patch(
                    "assent.folder_verification._receipt_matches_current_candidate_locked",
                    return_value=True), \
                mock.patch("assent.folder_verification.read_receipt", return_value=receipt), \
                mock.patch("assent.folder_verification.gitops.main_worktree",
                           return_value=self.root), \
                mock.patch("assent.folder_verification._verify_locked") as full:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify_folder_if_needed(self.cfg)

        self.assertEqual(result, 0)
        self.assertEqual(events, [
            "integration-enter", "folder-enter", "folder-exit", "integration-exit"])
        full.assert_not_called()

    def test_invalid_existing_receipt_fails_closed_without_full_suite(self):
        self.write_status("DONE")
        (self.tasks_dir / "_verification.toml").write_text(
            "not valid = [", encoding="utf-8")
        with mock.patch(
                "assent.folder_verification._receipt_matches_current_candidate_locked",
                side_effect=engine.AssentError("bad receipt")), \
                mock.patch("assent.folder_verification._verify_locked") as full:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify_folder_if_needed(self.cfg)
        self.assertEqual(result, 1)
        full.assert_not_called()

    def test_explicit_refresh_preserves_invalid_receipt_and_starts_no_candidate(self):
        self.write_status("DONE")
        path = self.tasks_dir / "_verification.toml"
        invalid = "not valid = ["
        path.write_text(invalid, encoding="utf-8")

        with mock.patch(
                "assent.folder_verification.gitops.temporary_integration_worktree") as candidate:
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
