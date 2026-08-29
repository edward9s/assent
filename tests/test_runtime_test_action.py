"""Source-bound runtime action and independent cursor tests."""
import _thread
import io
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from assent import AssentError
from assent.engine import _run_runtime_test_action, _runtime_test_prompt
from assent.plan import (
    RuntimeQuotaWait, WorkflowState, encode_runtime_action_results,
    parse_runtime_action_results, read_runtime_test_workflow_state,
    runtime_test_workflow_state_path, selection_workflow_state_path,
    workflow_state_path,
    write_runtime_test_workflow_state, write_workflow_state,
)


def python_command(source: str) -> str:
    arguments = [sys.executable, "-u", "-c", source]
    return (subprocess.list2cmdline(arguments) if os.name == "nt"
            else shlex.join(arguments))


class TestRuntimeTestAction(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "runtime@example.invalid"],
            cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Runtime Test"],
            cwd=self.root, check=True)
        (self.root / "source.txt").write_text("source\n", encoding="utf-8")
        subprocess.run(["git", "add", "source.txt"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", "fixture"], cwd=self.root,
            check=True)
        self.assent_dir = self.root / ".assent"
        self.owner = self.assent_dir / "plan01"
        self.owner.mkdir(parents=True)
        self.cfg = SimpleNamespace(
            root=self.root,
            git_excludes=(".assent/",))

    def state(self, **changes) -> WorkflowState:
        values = dict(
            unit="runtime_test", task_id="", step_index=0, started=False,
            action="runtime_test")
        values.update(changes)
        return WorkflowState(**values)

    def run_action(self, command: str | tuple[str, ...], state=None):
        commands = (command,) if isinstance(command, str) else command
        return _run_runtime_test_action(
            self.cfg, self.owner, commands, state or self.state())

    def test_exit_results_output_and_passed_reuse_are_authoritative(self):
        passed_command = python_command("print('runtime-visible')")
        output = io.StringIO()
        with redirect_stdout(output):
            state, record, reused = self.run_action(passed_command)
        self.assertFalse(reused)
        self.assertEqual((record.status, record.exit_code), ("PASSED", 0))
        self.assertIn("runtime-visible", output.getvalue())
        self.assertIn("runtime-visible", record.summary)

        with redirect_stdout(io.StringIO()):
            same_state, same_record, reused = self.run_action(
                passed_command, state)
        self.assertTrue(reused)
        self.assertEqual(same_record, record)
        self.assertEqual(same_state, state)

        failed_command = python_command(
            "import sys; print('x' * 5000); print('failure-evidence'); sys.exit(7)")
        with redirect_stdout(io.StringIO()):
            failed_state, failed, reused = self.run_action(
                failed_command, state)
        self.assertFalse(reused)
        self.assertEqual((failed.status, failed.exit_code), ("FAILED", 7))
        self.assertIn("failure-evidence", failed.summary)
        self.assertLessEqual(len(failed.summary), 4096)
        self.assertEqual(
            read_runtime_test_workflow_state(self.owner), failed_state)

    def test_commands_stop_at_first_failure_and_report_each_outcome(self):
        marker = self.root / "must-not-run"
        commands = (
            python_command("print('first-passed')"),
            python_command("import sys; print('second-failed'); sys.exit(7)"),
            python_command(
                f"from pathlib import Path; Path({str(marker)!r}).touch()"),
        )

        with redirect_stdout(io.StringIO()):
            state, record, reused = self.run_action(commands)

        self.assertFalse(reused)
        self.assertEqual((record.status, record.exit_code), ("FAILED", 7))
        self.assertEqual(
            record.results,
            ((commands[0], 0), (commands[1], 7), (commands[2], None)))
        self.assertFalse(marker.exists())
        self.assertIn("second-failed", record.summary)
        self.assertEqual(
            parse_runtime_action_results(state.action_evidence[0]),
            record.results)
        prompt = _runtime_test_prompt(state)
        self.assertIn(f"- {commands[1]}: exit 7", prompt)
        self.assertIn(f"- {commands[2]}: not run", prompt)

    def test_output_is_forwarded_before_process_exits(self):
        command = python_command(
            "import time; print('early', flush=True); time.sleep(2)")
        seen = threading.Event()
        finished = threading.Event()
        errors = []

        def capture(*values, **options):
            if "early" in options.get("end", "").join(map(str, values)):
                seen.set()

        def run():
            try:
                from unittest.mock import patch
                with patch("assent.engine.print", side_effect=capture):
                    self.run_action(command)
            except BaseException as error:
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(seen.wait(1.5))
        self.assertFalse(finished.is_set())
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])

    def test_source_and_command_changes_cannot_reuse_passed_evidence(self):
        original = python_command("print('first')")
        with redirect_stdout(io.StringIO()):
            state, first, _ = self.run_action(original)
            changed, second, reused = self.run_action(
                python_command("print('second')"), state)
        self.assertFalse(reused)
        self.assertEqual(second.status, "PASSED")
        self.assertNotEqual(first.identity, second.identity)

        mutating = python_command(
            "from pathlib import Path; Path('source.txt').write_text('changed')")
        with redirect_stdout(io.StringIO()):
            stale_state, stale, reused = self.run_action(mutating, changed)
        self.assertFalse(reused)
        self.assertEqual(stale.status, "STALE")
        self.assertEqual(
            read_runtime_test_workflow_state(self.owner), stale_state)
        self.assertEqual((self.root / "source.txt").read_text(encoding="utf-8"),
                         "changed")

    def test_launch_failure_is_persisted(self):
        with redirect_stdout(io.StringIO()):
            state, record, reused = self.run_action("embedded\0nul")
        self.assertFalse(reused)
        self.assertEqual(record.status, "FAILED")
        self.assertIn("Unable to start", record.summary)
        self.assertEqual(read_runtime_test_workflow_state(self.owner), state)

    def test_interrupt_reaps_child_and_preserves_stale_evidence(self):
        pid_file = self.root / "runtime.pid"
        self.cfg.git_excludes += ("runtime.pid",)
        command = python_command(
            "import os,time; from pathlib import Path; "
            "Path('runtime.pid').write_text(str(os.getpid())); "
            "\nwhile True: print('tick', flush=True); time.sleep(.05)")

        def interrupt_when_started():
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            _thread.interrupt_main()

        interrupter = threading.Thread(target=interrupt_when_started)
        interrupter.start()
        with self.assertRaises(KeyboardInterrupt), redirect_stdout(io.StringIO()):
            self.run_action(command)
        interrupter.join(1)
        state = read_runtime_test_workflow_state(self.owner)
        self.assertIsNotNone(state)
        self.assertEqual(state.action_status, "STALE")
        self.assertIn("interrupted", state.action_evidence[1].lower())
        pid = int(pid_file.read_text(encoding="utf-8"))
        if os.name == "nt":
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, check=False).stdout
            self.assertNotIn(f'"{pid}"', listing)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_plan_and_main_paths_and_quota_round_trip_are_independent(self):
        reset = datetime(2030, 4, 5, 6, 7, tzinfo=timezone.utc)
        plan_state = self.state(
            step_index=2,
            quota_waits=(RuntimeQuotaWait("alternate", reset),
                         RuntimeQuotaWait("primary")))
        main_state = self.state(step_index=4)
        write_runtime_test_workflow_state(self.owner, plan_state)
        write_runtime_test_workflow_state(self.assent_dir, main_state)
        self.assertNotEqual(runtime_test_workflow_state_path(self.owner),
                            runtime_test_workflow_state_path(self.assent_dir))
        self.assertEqual(read_runtime_test_workflow_state(self.owner), plan_state)
        self.assertEqual(read_runtime_test_workflow_state(self.assent_dir), main_state)
        self.assertFalse(workflow_state_path(self.owner).exists())
        self.assertFalse(selection_workflow_state_path(self.assent_dir).exists())

        write_workflow_state(
            self.owner, WorkflowState("task", "t001", 0, False))
        self.assertEqual(read_runtime_test_workflow_state(self.owner), plan_state)
        self.assertTrue(workflow_state_path(self.owner).exists())

        path = runtime_test_workflow_state_path(self.owner)
        path.write_text(path.read_text(encoding="utf-8").replace(
            "quota_waits =", "legacy_quota ="), encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "invalid schema"):
            read_runtime_test_workflow_state(self.owner)

        write_runtime_test_workflow_state(self.owner, plan_state)
        path.write_text(path.read_text(encoding="utf-8").replace(
            'candidate_head = ""\n', ""), encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "invalid schema"):
            read_runtime_test_workflow_state(self.owner)

    def test_runtime_state_preserves_the_exact_command_while_bounding_output(self):
        command = "python -c " + "x" * 5000
        encoded = encode_runtime_action_results(((command, 1),))
        state = self.state(
            action_status="FAILED", action_source_tree="source:command",
            action_exit_code=1, action_evidence=(encoded, "failed"))

        write_runtime_test_workflow_state(self.owner, state)

        self.assertEqual(
            read_runtime_test_workflow_state(self.owner).action_evidence[0],
            encoded)

    def test_runtime_state_rejects_passed_with_nonzero_exit(self):
        state = self.state(
            action_status="PASSED", action_source_tree="source:command",
            action_exit_code=1,
            action_evidence=(
                encode_runtime_action_results((("command", 1),)), "failed"))

        with self.assertRaisesRegex(AssentError, "invalid action evidence"):
            write_runtime_test_workflow_state(self.owner, state)

        valid = self.state(
            action_status="PASSED", action_source_tree="source:command",
            action_exit_code=0,
            action_evidence=(
                encode_runtime_action_results((("command", 0),)), "passed"))
        write_runtime_test_workflow_state(self.owner, valid)
        path = runtime_test_workflow_state_path(self.owner)
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "action_exit_code = 0", "action_exit_code = 1"),
            encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "invalid values"):
            read_runtime_test_workflow_state(self.owner)


if __name__ == "__main__":
    unittest.main()
