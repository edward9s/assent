"""claude adapter 測試:組命令、watchdog、額度偵測、檔位翻譯。

全部用假子程序(sys.executable -c ...)或直接餵字串給純函式——不打真實 claude CLI、
不依賴網路(鐵則 4)。真 CLI 探勘只在 W1 一次性錄 fixture,見 stream_json_ok.txt。
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
    """建立測試用 Config;預設檔位對照表沿用內建(prime→fable 等)。"""
    base = dict(root=Path("."), agents_dir=Path("./.agents"),
                tasks_dir=Path("./.agents/plan01"), tasks_name="plan01")
    base.update(overrides)
    return Config(**base)


class TestBuildCommand(unittest.TestCase):
    def test_includes_verbose_and_stream_json(self):
        # 探勘實證:stream-json 必須配 --verbose,adapter 一律注入
        cmd = build_command(make_cfg(), "做事", "fable", "high")
        self.assertIn("--verbose", cmd)
        i = cmd.index("--output-format")
        self.assertEqual(cmd[i + 1], "stream-json")

    def test_model_effort_and_prompt_placement(self):
        cmd = build_command(make_cfg(claude_command="claude.cmd"),
                            "提示詞", "opus", "low")
        self.assertEqual(cmd[0], "claude.cmd")
        self.assertEqual(cmd[cmd.index("-p") + 1], "提示詞")
        self.assertEqual(cmd[cmd.index("--model") + 1], "opus")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

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
        # 成功 fixture 內含 "rate_limit_event"/"rateLimitType" 字樣但 status=allowed,
        # 絕不可誤判為額度耗盡
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
        # 無結構化 blocked status,只有人類可讀文字命中 → 仍判額度耗盡,reset 無法解析
        line = json.dumps({"type": "result", "subtype": "error_max_turns",
                           "result": "Usage limit reached. Try again later."})
        exhausted, reset_at = parse_output_for_quota(line + "\n")
        self.assertTrue(exhausted)
        self.assertIsNone(reset_at)

    def test_text_fallback_ignores_raw_json_key_names(self):
        # 光是出現 result 型別、內文正常,不該因 JSON 裡的鍵名(如含 limit 的欄位)誤觸
        line = json.dumps({"type": "result", "result": "完成任務,一切正常"})
        exhausted, _ = parse_output_for_quota(line + "\n")
        self.assertFalse(exhausted)

    def test_non_json_stderr_line_can_trigger_text_fallback(self):
        exhausted, _ = parse_output_for_quota("Error: rate limit exceeded\n")
        self.assertTrue(exhausted)

    def test_w5_real_session_limit_message_detected(self):
        # W5 真實撞限實測訊息(2026-07-15,Pro 訂閱):舊 regex 漏接的樣式
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
        self.assertNotEqual(rc, 0)          # 被殺 → 非零退出
        self.assertLess(elapsed, 10)        # 沒有等滿 30 秒

    def test_watchdog_disabled_does_not_hang_on_quick_process(self):
        rc, out, stalled = run_subprocess(
            _py("print('quick')"), Path("."), stall_seconds=0)  # 0 = 停用 watchdog
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
            raise RuntimeError("顯示層爆炸")
        rc, out, stalled = run_subprocess(
            _py("print('still here')"), Path("."), stall_seconds=10, echo=bad_echo)
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIn("still here", out)


class TestFormatStreamEvent(unittest.TestCase):
    def test_assistant_text_and_tool_use(self):
        line = json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "我先讀計畫檔"},
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": "C:\\plans\\TEST_PLAN.md"}}]}})
        rendered = format_stream_event(line)
        self.assertIn("AI| 我先讀計畫檔", rendered)
        self.assertIn("工具| Read C:\\plans\\TEST_PLAN.md", rendered)

    def test_result_shows_output_tokens(self):
        line = json.dumps({"type": "result", "subtype": "success",
                           "duration_ms": 2551, "usage": {"output_tokens": 142115}})
        rendered = format_stream_event(line)
        self.assertIn("142115", rendered)
        self.assertIn("結束", rendered)

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
            "提示", requested_model, "high", Path("/proj"))
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(result.exit_code, 0)
        self.assertFalse(result.quota_exhausted)
        # prime → fable(內建對照);且命令帶上 alias 與 effort
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
        # 停滯回傳的輸出即使含額度字樣,也一律當任務失敗、絕不誤判額度(2.5)
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
