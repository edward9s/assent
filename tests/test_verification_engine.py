"""Scheduler handoff tests for unattended folder verification."""
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
from assent.verification import (
    VerificationReceipt, _run_full_verifier, _verify_locked,
    verify_folder_if_needed,
)


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


class TestRunVerificationHandoff(VerificationEngineCase):
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
                mock.patch("assent.engine._try_write_report"):
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

        with mock.patch("assent.verification.hold_integration_lock",
                        integration_lock), \
                mock.patch("assent.verification.hold_lock", folder_lock), \
                mock.patch(
                    "assent.verification._receipt_matches_current_candidate_locked",
                    return_value=True), \
                mock.patch("assent.verification.read_receipt", return_value=receipt), \
                mock.patch("assent.verification.gitops.main_worktree",
                           return_value=self.root), \
                mock.patch("assent.verification._verify_locked") as full:
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
                "assent.verification._receipt_matches_current_candidate_locked",
                side_effect=engine.AssentError("bad receipt")), \
                mock.patch("assent.verification._verify_locked") as full:
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
                "assent.verification.gitops.temporary_integration_worktree") as candidate:
            with self.assertRaises(engine.AssentError):
                _verify_locked(self.cfg)

        self.assertEqual(path.read_text(encoding="utf-8"), invalid)
        candidate.assert_not_called()


class TestVerificationPrompt(VerificationEngineCase):
    def test_timeout_is_not_defined_as_sufficient_for_blocked(self):
        self.write_status("TODO")
        task = engine.Plan.parse(self.tasks_dir).tasks[0]
        session = engine._SessionIdentity(
            agent="codex", requested_model="model", effort="high",
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
        with mock.patch("assent.verification.subprocess.run",
                        return_value=completed) as run_child, \
                mock.patch("assent.verification.time.monotonic",
                           side_effect=[10.0, 311.25]), \
                contextlib.redirect_stdout(output):
            actual = _run_full_verifier(
                Path("verify.py"), Path("candidate with spaces"))

        self.assertIs(actual, completed)
        self.assertNotIn("timeout", run_child.call_args.kwargs)
        self.assertEqual(run_child.call_args.kwargs["cwd"],
                         "candidate with spaces")
        self.assertIn("Full verification started", output.getvalue())
        self.assertIn("elapsed 301.2s, exit code 7", output.getvalue())

    def test_interrupt_is_reported_and_propagated_for_candidate_cleanup(self):
        output = io.StringIO()
        with mock.patch("assent.verification.subprocess.run",
                        side_effect=KeyboardInterrupt), \
                mock.patch("assent.verification.time.monotonic",
                           side_effect=[20.0, 325.0]), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(KeyboardInterrupt):
            _run_full_verifier(Path("verify.py"), Path("candidate"))

        self.assertIn("elapsed 305.0s, exit code 130", output.getvalue())


if __name__ == "__main__":
    unittest.main()
