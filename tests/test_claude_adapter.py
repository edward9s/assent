"""claude adapter tests: command construction, the watchdog, quota detection, tier resolution.

Everything uses a fake subprocess (sys.executable -c ...) or feeds strings directly to pure
functions — never a real claude CLI, never the network (ground rule 4). The real CLI was
probed once to record a fixture; see stream_json_ok.txt.
"""
import json
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agents import AgentsError
from agents.adapters import TaskResult, get_adapter
from agents.adapters.claude import (
    ClaudeAdapter, build_command, format_stream_event, parse_output_for_quota,
    run_subprocess)
from agents.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_cfg(**overrides) -> Config:
    """Build a test Config; the default tier mapping uses the built-ins (prime -> fable, etc.)."""
    base = dict(root=Path("."), agents_dir=Path("./.agents"),
                tasks_dir=Path("./.agents/plan01"), tasks_name="plan01")
    base.update(overrides)
    return Config(**base)


class TestBuildCommand(unittest.TestCase):
    def test_includes_verbose_and_stream_json(self):
        # Found by probing: stream-json must be paired with --verbose; the adapter always injects it
        cmd = build_command(make_cfg(), "do the task", "fable", "high")
        self.assertIn("--verbose", cmd)
        i = cmd.index("--output-format")
        self.assertEqual(cmd[i + 1], "stream-json")

    def test_model_effort_and_prompt_placement(self):
        cmd = build_command(make_cfg(claude_command="claude.cmd"),
                            "the prompt", "opus", "max")
        self.assertEqual(cmd[0], "claude.cmd")
        self.assertEqual(cmd[cmd.index("-p") + 1], "the prompt")
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
            "agents.adapters.claude", fromlist=["run_subprocess"]).run_subprocess

    def patch_run(self, fake):
        import agents.adapters.claude as mod
        mod.run_subprocess = fake
        self.addCleanup(setattr, mod, "run_subprocess", self._orig)

    def test_translates_tier_to_alias_and_returns_result(self):
        captured = {}

        def fake(cmd, cwd, stall_seconds, echo=None):
            captured["cmd"] = cmd
            captured["cwd"] = cwd
            return 0, '{"type":"result","result":"OK"}\n', False

        self.patch_run(fake)
        adapter = ClaudeAdapter(make_cfg())
        requested_model = adapter.resolve_model("prime")
        result = adapter.run_task(
            "prompt", requested_model, "high", Path("/proj"))
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.quota_exhausted)
        # prime -> fable (built-in mapping); the command carries both the alias and the effort
        self.assertEqual(
            captured["cmd"][captured["cmd"].index("--model") + 1],
            requested_model)
        self.assertEqual(captured["cmd"][captured["cmd"].index("--effort") + 1], "high")
        self.assertEqual(captured["cwd"], Path("/proj"))

    def test_unknown_tier_raises(self):
        adapter = ClaudeAdapter(make_cfg(claude_models={"core": "opus"}))
        with self.assertRaises(AgentsError):
            adapter.resolve_model("prime")

    def test_quota_output_sets_flags(self):
        ts = 1784041800
        quota_line = json.dumps({"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": ts}}) + "\n"
        self.patch_run(lambda c, w, s, echo=None: (0, quota_line, False))
        adapter = ClaudeAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve_model("lite"), None, Path("."))
        self.assertTrue(result.quota_exhausted)
        self.assertEqual(result.reset_at, datetime.fromtimestamp(ts, tz=timezone.utc))

    def test_stall_is_failure_not_quota(self):
        # Even if a stall's output contains quota-looking text, it's always a task failure,
        # never mistaken for quota exhaustion (2.5)
        self.patch_run(lambda c, w, s, echo=None: (1, "rate limit exceeded\n", True))
        adapter = ClaudeAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve_model("lite"), None, Path("."))
        self.assertFalse(result.quota_exhausted)
        self.assertIsNone(result.reset_at)
        self.assertNotEqual(result.exit_code, 0)


class TestGetAdapter(unittest.TestCase):
    def test_claude_returns_adapter(self):
        self.assertIsInstance(get_adapter("claude", make_cfg()), ClaudeAdapter)

    def test_unknown_name_raises(self):
        with self.assertRaises(AgentsError):
            get_adapter("definitely-unknown", make_cfg())


if __name__ == "__main__":
    unittest.main()
