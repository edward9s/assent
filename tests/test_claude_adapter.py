"""claude adapter tests: command construction, the watchdog, quota detection, tier resolution.

Everything uses a fake subprocess (sys.executable -c ...) or feeds strings directly to pure
functions — never a real claude CLI, never the network (ground rule 4). The real CLI was
probed once to record a fixture; see stream_json_ok.txt.
"""
import hashlib
import json
import subprocess
import sys
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.adapters import CHECKPOINT_RESUME_RECORD, TaskResult, get_adapter
from assent.adapters import process as process_runner
from assent.adapters.claude import (
    ClaudeAdapter, build_command, format_stream_event, parse_output_for_billing,
    parse_output_for_quota, run_subprocess)
from assent.adapters.process import clear_stop_wake, wake_stop_waiters
from assent.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_cfg(**overrides) -> Config:
    """Build a test Config; the default tier mapping uses the built-ins (prime -> fable, etc.)."""
    base = dict(root=Path("."), assent_dir=Path("./.assent"),
                tasks_dir=Path("./.assent/plan01"), tasks_name="plan01")
    base.update(overrides)
    return Config(**base)


class TestBuildCommand(unittest.TestCase):
    def test_includes_verbose_and_stream_json(self):
        # Found by probing: stream-json must be paired with --verbose; the adapter always injects it
        cmd = build_command(make_cfg(), "do the task", "fable", "high")
        self.assertIn("--verbose", cmd)
        i = cmd.index("--output-format")
        self.assertEqual(cmd[i + 1], "stream-json")

    def test_model_effort_and_prompt_via_stdin(self):
        cmd = build_command(make_cfg(claude_command="claude.cmd"),
                            "the prompt", "opus", "max")
        self.assertEqual(cmd[0], "claude.cmd")
        self.assertIn("-p", cmd)
        self.assertNotIn("the prompt", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "max")

    def test_effort_omitted_when_none(self):
        cmd = build_command(make_cfg(), "x", "sonnet", None)
        self.assertNotIn("--effort", cmd)

    def test_extra_args_appended_verbatim(self):
        cfg = make_cfg(claude_extra_args=["--permission-mode", "acceptEdits",
                                          "--add-dir", "D:\\docs"])
        cmd = build_command(cfg, "x", "fable", "high")
        self.assertEqual(cmd[-4:], ["--permission-mode", "acceptEdits",
                                    "--add-dir", "D:\\docs"])


class TestParseQuota(unittest.TestCase):
    def test_real_ok_fixture_is_not_quota(self):
        # The success fixture contains the literal strings "rate_limit_event"/"rateLimitType"
        # but status=allowed; this must never be mistaken for quota exhaustion
        output = (FIXTURES / "stream_json_ok.txt").read_text(encoding="utf-8")
        exhausted, reset_at = parse_output_for_quota(output)
        self.assertFalse(exhausted)
        self.assertIsNone(reset_at)

    def test_blocked_status_with_reset_timestamp(self):
        ts = 1784041800
        line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": ts, "rateLimitType": "five_hour"}})
        exhausted, reset_at = parse_output_for_quota(line + "\n")
        self.assertTrue(exhausted)
        self.assertEqual(reset_at, datetime.fromtimestamp(ts, tz=timezone.utc))

    def test_blocked_status_without_reset_time(self):
        line = json.dumps({"type": "rate_limit_event",
                           "rate_limit_info": {"status": "blocked"}})
        exhausted, reset_at = parse_output_for_quota(line + "\n")
        self.assertTrue(exhausted)
        self.assertIsNone(reset_at)

    def test_text_fallback_from_result_message(self):
        # No structured blocked status, only a human-readable text hit -> still judged
        # exhausted; reset can't be parsed
        line = json.dumps({"type": "result", "subtype": "error_max_turns",
                           "result": "Usage limit reached. Try again later."})
        exhausted, reset_at = parse_output_for_quota(line + "\n")
        self.assertTrue(exhausted)
        self.assertIsNone(reset_at)

    def test_text_fallback_ignores_raw_json_key_names(self):
        # A plain result type with ordinary body text must not false-positive just because
        # a JSON key name (e.g. one containing "limit") appears somewhere
        line = json.dumps({"type": "result", "result": "Task complete, everything is fine"})
        exhausted, _ = parse_output_for_quota(line + "\n")
        self.assertFalse(exhausted)

    def test_non_json_stderr_line_can_trigger_text_fallback(self):
        exhausted, _ = parse_output_for_quota("Error: rate limit exceeded\n")
        self.assertTrue(exhausted)

    def test_real_session_limit_message_detected(self):
        # Message observed from a real quota hit (2026-07-15, Pro subscription): a pattern
        # the older regex used to miss
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text",
             "text": "You've hit your session limit · resets 4am (Asia/Taipei)"}]}})
        exhausted, _ = parse_output_for_quota(line + "\n")
        self.assertTrue(exhausted)

    def test_session_limit_variants_detected(self):
        for text in ("Session limit reached", "you've hit your weekly limit",
                     "You have hit your usage limit."):
            line = json.dumps({"type": "result", "result": text})
            exhausted, _ = parse_output_for_quota(line + "\n")
            self.assertTrue(exhausted, text)

    def test_garbage_lines_are_tolerated(self):
        exhausted, reset_at = parse_output_for_quota(
            "\n\nnot json at all\n{broken json\n")
        self.assertFalse(exhausted)
        self.assertIsNone(reset_at)


class TestCheckpointResume(unittest.TestCase):
    def test_exact_final_record_is_recognized_and_not_rendered(self):
        from assent.adapters import parse_checkpoint_resume_output

        output = "partial output\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.assertTrue(parse_checkpoint_resume_output(output, 1, False))
        self.assertIsNone(format_stream_event(CHECKPOINT_RESUME_RECORD + "\n"))

    def test_zero_exit_stall_and_nonfinal_or_lookalike_records_are_rejected(self):
        from assent.adapters import parse_checkpoint_resume_output

        cases = (
            (0, CHECKPOINT_RESUME_RECORD + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD + "\n", True),
            (1, "prefix " + CHECKPOINT_RESUME_RECORD + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD[:-1] + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD + "\ntrailing\n", False),
            (1, CHECKPOINT_RESUME_RECORD + " \n", False),
            (1, '{"type": "assent.checkpoint_resume"}\n', False),
        )
        for exit_code, output, stalled in cases:
            with self.subTest(exit_code=exit_code, output=output, stalled=stalled):
                self.assertFalse(
                    parse_checkpoint_resume_output(output, exit_code, stalled))


class TestParseBilling(unittest.TestCase):
    def test_real_ok_fixture_is_not_billing(self):
        # A normal successful session never looks like a billing failure
        output = (FIXTURES / "stream_json_ok.txt").read_text(encoding="utf-8")
        self.assertFalse(parse_output_for_billing(output))

    def test_recorded_billing_fixture_is_detected(self):
        # The reproduced live shape: a result event with api_error_status 400 and
        # "Credit balance is too low" in the result text (see stream_json_billing.txt)
        output = (FIXTURES / "stream_json_billing.txt").read_text(encoding="utf-8")
        self.assertTrue(parse_output_for_billing(output))
        # billing is not quota: the wait-and-resume path must never be taken for it
        exhausted, _ = parse_output_for_quota(output)
        self.assertFalse(exhausted)

    def test_api_error_status_402_with_billing_text_is_detected(self):
        line = json.dumps({"type": "result", "subtype": "success",
                           "api_error_status": 402,
                           "result": "Insufficient credit balance"})
        self.assertTrue(parse_output_for_billing(line + "\n"))

    def test_billing_text_without_api_status_still_detected_via_fallback(self):
        # Plain human-readable text is enough even when the structured status is absent
        line = json.dumps({"type": "result",
                           "result": "Payment required to continue"})
        self.assertTrue(parse_output_for_billing(line + "\n"))
        self.assertTrue(parse_output_for_billing("Error: your balance is too low\n"))

    def test_ordinary_result_and_non_billing_status_do_not_false_positive(self):
        ok = json.dumps({"type": "result", "result": "Task complete, all good"})
        self.assertFalse(parse_output_for_billing(ok + "\n"))
        # a 400 without billing text is some other bad request, not a balance problem
        other = json.dumps({"type": "result", "api_error_status": 400,
                            "result": "malformed request payload"})
        self.assertFalse(parse_output_for_billing(other + "\n"))


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class TestRunSubprocess(unittest.TestCase):
    def test_normal_collects_output_and_exit_code(self):
        rc, out, stalled = run_subprocess(
            _py("print('line1'); print('line2')"), Path("."), stall_seconds=10)
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIn("line1", out)
        self.assertIn("line2", out)

    def test_non_zero_exit_code_propagates(self):
        rc, _out, stalled = run_subprocess(
            _py("import sys; print('bye'); sys.exit(3)"), Path("."), stall_seconds=10)
        self.assertEqual(rc, 3)
        self.assertFalse(stalled)

    def test_watchdog_kills_stalled_process(self):
        start = time.monotonic()
        rc, _out, stalled = run_subprocess(
            _py("import time; time.sleep(30)"), Path("."), stall_seconds=0.3)
        elapsed = time.monotonic() - start
        self.assertTrue(stalled)
        self.assertNotEqual(rc, 0)          # Killed -> non-zero exit
        self.assertLess(elapsed, 10)        # Did not wait out the full 30 seconds

    def test_watchdog_disabled_does_not_hang_on_quick_process(self):
        rc, out, stalled = run_subprocess(
            _py("print('quick')"), Path("."), stall_seconds=0)  # 0 = watchdog disabled
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIn("quick", out)

    def test_echo_receives_each_line_live(self):
        got = []
        rc, out, _ = run_subprocess(
            _py("print('line1'); print('line2')"), Path("."),
            stall_seconds=10, echo=got.append)
        self.assertEqual(rc, 0)
        self.assertEqual([g.strip() for g in got], ["line1", "line2"])

    def test_echo_exception_does_not_break_collection(self):
        def bad_echo(_line):
            raise RuntimeError("display layer exploded")
        rc, out, stalled = run_subprocess(
            _py("print('still here')"), Path("."), stall_seconds=10, echo=bad_echo)
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIn("still here", out)

    def test_large_utf8_input_is_delivered_exactly_while_output_streams(self):
        payload = ("ASCII line\n臺灣 mixed\r\n" * 12_000)
        expected = payload.encode("utf-8")
        script = (
            "import hashlib, sys\n"
            "data = bytearray()\n"
            "while True:\n"
            "    chunk = sys.stdin.buffer.read(4096)\n"
            "    if not chunk:\n"
            "        break\n"
            "    data.extend(chunk)\n"
            "    print('chunk', flush=True)\n"
            "print(len(data))\n"
            "print(hashlib.sha256(data).hexdigest())\n"
        )
        command = _py(script)
        rc, out, stalled = run_subprocess(
            command, Path("."), stall_seconds=10, input_text=payload)
        lines = out.splitlines()
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertGreaterEqual(len(expected), 100 * 1024)
        self.assertLess(sum(len(arg) for arg in command), 4096)
        self.assertEqual(int(lines[-2]), len(expected))
        self.assertEqual(lines[-1], hashlib.sha256(expected).hexdigest())

    def test_early_child_exit_does_not_leave_input_writer_hanging(self):
        script = (
            "import sys\n"
            "sys.stdin.close()\n"
            "print('closed', flush=True)\n"
            "sys.exit(7)\n"
        )
        rc, out, stalled = run_subprocess(
            _py(script), Path("."), stall_seconds=10,
            input_text="prompt data\n" * 50_000)
        self.assertEqual(rc, 7)
        self.assertFalse(stalled)
        self.assertIn("closed", out)


class TestRunSubprocessStopWake(unittest.TestCase):
    """A stop request must end output collection at once.

    The queue has no timeout when the watchdog is disabled, so a silent CLI used
    to keep the whole work-folder process -- and therefore its task-folder lock
    -- alive indefinitely after the scheduler had asked it to stop.
    """

    # Comfortably longer than starting a process, far shorter than the child's
    # own sleep, so a hang fails the test instead of slowing it down.
    _BOUND = 30

    def setUp(self):
        self.addCleanup(clear_stop_wake)

    def wake_soon(self) -> None:
        """Stand in for the stdin watcher waking the collection mid-run."""
        timer = threading.Timer(0.5, wake_stop_waiters)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)

    def test_wake_ends_a_silent_child_with_the_watchdog_disabled(self):
        children: list[subprocess.Popen] = []
        real_popen = subprocess.Popen

        def spy(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child

        self.wake_soon()
        started = time.monotonic()
        with mock.patch.object(process_runner.subprocess, "Popen", spy):
            with self.assertRaises(KeyboardInterrupt):
                run_subprocess(_py("import time; time.sleep(300)"), Path("."),
                               stall_seconds=0)  # 0 = watchdog disabled

        self.assertLess(time.monotonic() - started, self._BOUND)
        # Killed and reaped before the interrupt propagated: no AI descendant
        # and no zombie survives the session.
        self.assertIsNotNone(children[0].poll())

    def test_wake_ends_a_child_the_watchdog_would_still_be_waiting_for(self):
        self.wake_soon()
        started = time.monotonic()
        with self.assertRaises(KeyboardInterrupt):
            run_subprocess(_py("import time; time.sleep(300)"), Path("."),
                           stall_seconds=300)
        self.assertLess(time.monotonic() - started, self._BOUND)

    def test_stale_stop_request_does_not_shorten_a_later_run(self):
        """One stop request must not make every later in-process adapter run
        return early."""
        wake_stop_waiters()
        rc, out, stalled = run_subprocess(
            _py("print('ok')"), Path("."), stall_seconds=10)
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIn("ok", out)


class TestFormatStreamEvent(unittest.TestCase):
    def test_assistant_text_and_tool_use(self):
        # The assistant text is opaque upstream fixture data (Chinese kept on purpose to
        # prove Unicode passthrough); it must render verbatim, never translated.
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "我先讀計畫檔"},
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "C:\\plans\\TEST_PLAN.md"}}]}})
        rendered = format_stream_event(line)
        self.assertIn("AI| 我先讀計畫檔", rendered)
        self.assertIn("Tool| Read C:\\plans\\TEST_PLAN.md", rendered)

    def test_result_shows_output_tokens(self):
        line = json.dumps({"type": "result", "subtype": "success",
                           "duration_ms": 2551, "usage": {"output_tokens": 142115}})
        rendered = format_stream_event(line)
        self.assertIn("142115", rendered)
        self.assertIn("ended", rendered)

    def test_allowed_rate_limit_event_is_silent(self):
        line = json.dumps({"type": "rate_limit_event",
                           "rate_limit_info": {"status": "allowed"}})
        self.assertIsNone(format_stream_event(line))

    def test_blocked_rate_limit_event_is_shown(self):
        line = json.dumps({"type": "rate_limit_event",
                           "rate_limit_info": {"status": "rejected"}})
        self.assertIn("rejected", format_stream_event(line))

    def test_non_json_line_passthrough_and_blank_hidden(self):
        self.assertIn("boom", format_stream_event("Error: boom"))
        self.assertIsNone(format_stream_event("   \n"))


class TestRunTask(unittest.TestCase):
    def setUp(self):
        self._orig = __import__(
            "assent.adapters.claude", fromlist=["run_subprocess"]).run_subprocess

    def patch_run(self, fake):
        import assent.adapters.claude as mod
        mod.run_subprocess = fake
        self.addCleanup(setattr, mod, "run_subprocess", self._orig)

    def test_translates_tier_to_alias_and_returns_result(self):
        captured = {}

        def fake(cmd, cwd, stall_seconds, echo=None, input_text=None):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            captured["input_text"] = input_text
            return 0, '{"type":"result","result":"OK"}\n', False

        self.patch_run(fake)
        adapter = ClaudeAdapter(make_cfg())
        requested_model = adapter.resolve_model("prime")
        result = adapter.run_task(
            "prompt", requested_model, "high", Path("/proj"))
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.quota_exhausted)
        self.assertFalse(result.stalled)
        # prime -> fable (built-in mapping); the command carries both the alias and the effort
        self.assertEqual(
            captured["cmd"][captured["cmd"].index("--model") + 1],
            requested_model)
        self.assertEqual(captured["cmd"][captured["cmd"].index("--effort") + 1], "high")
        self.assertEqual(captured["cwd"], Path("/proj"))
        self.assertEqual(captured["input_text"], "prompt")

    def test_unknown_tier_raises(self):
        adapter = ClaudeAdapter(make_cfg(claude_models={"core": "opus"}))
        with self.assertRaises(AssentError):
            adapter.resolve_model("prime")

    def test_quota_output_sets_flags(self):
        ts = 1784041800
        quota_line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": ts}}) + "\n"
        self.patch_run(lambda c, w, s, echo=None, input_text=None: (1, quota_line, False))
        adapter = ClaudeAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve_model("lite"), None, Path("."))
        self.assertTrue(result.quota_exhausted)
        self.assertEqual(result.reset_at, datetime.fromtimestamp(ts, tz=timezone.utc))

    def test_quota_like_assistant_prose_on_success_is_not_quota(self):
        for phrase in ("quota exceeded", "rate limit", "session limit"):
            with self.subTest(phrase=phrase):
                line = json.dumps({"type": "assistant", "message": {
                    "content": [{"type": "text",
                                 "text": f"The answer discusses {phrase}."}]}})
                self.patch_run(lambda c, w, s, echo=None, input_text=None: (0, line + "\n", False))
                result = ClaudeAdapter(make_cfg()).run_task(
                    "p", "fable", None, Path("."))
                self.assertEqual(result.exit_code, 0)
                self.assertFalse(result.quota_exhausted)
                self.assertIsNone(result.reset_at)

    def test_billing_output_sets_failure_kind_not_quota(self):
        output = (FIXTURES / "stream_json_billing.txt").read_text(encoding="utf-8")
        # the real CLI exits non-zero on this api_error
        self.patch_run(lambda c, w, s, echo=None, input_text=None: (1, output, False))
        adapter = ClaudeAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve_model("lite"), None, Path("."))
        self.assertFalse(result.quota_exhausted)
        self.assertEqual(result.failure_kind, "billing")
        self.assertIsNone(result.reset_at)

    def test_billing_text_on_a_successful_exit_is_not_flagged(self):
        # billing is a failure classification: an exit-0 session whose prose mentions a
        # credit balance must never be classified as a billing failure
        line = json.dumps({"type": "result",
                           "result": "I checked the credit balance module, all good"})
        self.patch_run(lambda c, w, s, echo=None, input_text=None: (0, line + "\n", False))
        adapter = ClaudeAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve_model("lite"), None, Path("."))
        self.assertIsNone(result.failure_kind)
        self.assertFalse(result.quota_exhausted)

    def test_stall_is_failure_not_quota(self):
        # Even if a stall's output contains quota-looking text, it's always a task failure,
        # never mistaken for quota exhaustion (2.5)
        self.patch_run(lambda c, w, s, echo=None, input_text=None: (1, "rate limit exceeded\n", True))
        adapter = ClaudeAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve_model("lite"), None, Path("."))
        self.assertFalse(result.quota_exhausted)
        self.assertTrue(result.stalled)
        self.assertIsNone(result.reset_at)
        self.assertNotEqual(result.exit_code, 0)

    def test_checkpoint_resume_record_sets_distinct_result_and_keeps_raw_output(self):
        output = "partial\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = ClaudeAdapter(make_cfg()).run_task(
            "p", "fable", "medium", Path("."))
        self.assertTrue(result.checkpoint_resume)
        self.assertFalse(result.quota_exhausted)
        self.assertEqual(result.output, output)
        self.assertIsNone(result.failure_kind)

    def test_quota_and_control_record_use_the_quota_path(self):
        quota = json.dumps({"type": "rate_limit_event",
                            "rate_limit_info": {"status": "rejected"}})
        output = quota + "\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = ClaudeAdapter(make_cfg()).run_task(
            "p", "fable", "medium", Path("."))
        self.assertTrue(result.quota_exhausted)
        self.assertFalse(result.checkpoint_resume)

    def test_terminal_record_overrides_preceding_billing_prose(self):
        prose = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "I checked the credit balance report."}]}})
        output = prose + "\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = ClaudeAdapter(make_cfg()).run_task(
            "p", "fable", "medium", Path("."))
        self.assertTrue(result.checkpoint_resume)
        self.assertFalse(result.quota_exhausted)
        self.assertIsNone(result.failure_kind)

    def test_structured_task_extracts_result_event_text_not_raw_stream(self):
        final = '{"type":"assent.auto_fix_review","verdict":"PASS","findings":[]}'
        stream = (
            json.dumps({"type": "system", "subtype": "init"}) + "\n"
            + json.dumps({"type": "assistant", "message": {"content": [
                {"type": "text", "text": final}]}}) + "\n"
            + json.dumps({"type": "result", "subtype": "success",
                         "result": final}) + "\n")
        self.patch_run(lambda *args, **kwargs: (0, stream, False))
        result = ClaudeAdapter(make_cfg()).run_structured_task(
            "p", "opus", "medium", Path("."))
        self.assertEqual(result.output, stream)
        self.assertEqual(result.structured_output, final)
        self.assertIsNone(result.structured_output_error)

    def test_structured_task_errors_when_no_result_event_is_present(self):
        stream = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "no terminal result event here"}]}}) + "\n"
        self.patch_run(lambda *args, **kwargs: (0, stream, False))
        result = ClaudeAdapter(make_cfg()).run_structured_task(
            "p", "opus", "medium", Path("."))
        self.assertIsNone(result.structured_output)
        self.assertIsNotNone(result.structured_output_error)

    def test_structured_task_errors_when_result_field_is_blank(self):
        stream = json.dumps({"type": "result", "subtype": "success",
                             "result": "   "}) + "\n"
        self.patch_run(lambda *args, **kwargs: (0, stream, False))
        result = ClaudeAdapter(make_cfg()).run_structured_task(
            "p", "opus", "medium", Path("."))
        self.assertIsNone(result.structured_output)
        self.assertIsNotNone(result.structured_output_error)


class TestGetAdapter(unittest.TestCase):
    def test_claude_returns_adapter(self):
        self.assertIsInstance(get_adapter("claude", make_cfg()), ClaudeAdapter)

    def test_unknown_name_raises(self):
        with self.assertRaises(AssentError):
            get_adapter("definitely-unknown", make_cfg())


if __name__ == "__main__":
    unittest.main()
