"""Codex adapter tests; no network or real Codex session is used."""
import json
import unittest
from pathlib import Path

from agents import AgentsError
from agents.adapters import TaskResult, get_adapter
from agents.adapters.codex import (
    CodexAdapter, build_command, format_stream_event, parse_output_for_quota,
)
from agents.config import Config

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_cfg(**overrides) -> Config:
    values = dict(root=Path("."), agents_dir=Path("./.agents"),
                  tasks_dir=Path("./.agents/plan01"), tasks_name="plan01",
                  adapter_name="codex")
    values.update(overrides)
    return Config(**values)


class TestBuildCommand(unittest.TestCase):
    def test_json_model_effort_sandbox_and_prompt(self):
        cmd = build_command(make_cfg(), "提示詞", "gpt-5.6-sol", "high")
        self.assertEqual(cmd[:3], ["codex", "exec", "--json"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertEqual(cmd[-3:], ["--sandbox", "workspace-write", "提示詞"])

    def test_effort_can_be_omitted_and_extra_args_are_verbatim(self):
        cfg = make_cfg(codex_command="codex.cmd",
                       codex_extra_args=["--sandbox", "danger-full-access"])
        cmd = build_command(cfg, "p", "custom", None)
        self.assertEqual(cmd[0], "codex.cmd")
        self.assertNotIn("model_reasoning_effort", " ".join(cmd))
        self.assertEqual(cmd[-3:], ["--sandbox", "danger-full-access", "p"])


class TestFormatStreamEvent(unittest.TestCase):
    def test_real_cli_probe_fixture_contains_ai_message_and_usage(self):
        lines = (FIXTURES / "codex_json_ok.txt").read_text(
            encoding="utf-8").splitlines()
        rendered = [format_stream_event(line) for line in lines]
        self.assertTrue(any(text and "AI| OK" in text for text in rendered))
        self.assertTrue(any(text and "5 tokens" in text for text in rendered))
        self.assertFalse(parse_output_for_quota("\n".join(lines)))

    def test_agent_message_is_displayed(self):
        event = {"type": "item.completed", "item": {
            "id": "i1", "type": "agent_message", "text": "第一行\n第二行"}}
        rendered = format_stream_event(json.dumps(event, ensure_ascii=False))
        self.assertIn("AI| 第一行", rendered)
        self.assertIn("AI| 第二行", rendered)

    def test_tool_and_usage_events_are_displayed(self):
        tool = {"type": "item.started", "item": {
            "type": "command_execution", "command": "python -m unittest",
            "status": "in_progress"}}
        self.assertIn("python -m unittest", format_stream_event(json.dumps(tool)))
        done = {"type": "turn.completed", "usage": {
            "output_tokens": 12, "reasoning_output_tokens": 3}}
        rendered = format_stream_event(json.dumps(done))
        self.assertIn("12", rendered)
        self.assertIn("結束", rendered)

    def test_failure_error_and_non_json_are_displayed(self):
        failed = {"type": "turn.failed", "error": {"message": "boom"}}
        self.assertIn("boom", format_stream_event(json.dumps(failed)))
        self.assertIn("warning", format_stream_event("warning"))
        self.assertIsNone(format_stream_event("  \n"))


class TestQuota(unittest.TestCase):
    def test_error_and_agent_limit_messages_are_detected(self):
        for event in (
            {"type": "error", "message": "Usage limit reached"},
            {"type": "turn.failed", "error": {"message": "rate limit exceeded"}},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "You've hit your session limit"}},
        ):
            with self.subTest(event=event):
                self.assertTrue(parse_output_for_quota(json.dumps(event)))

    def test_tool_text_and_normal_completion_do_not_false_positive(self):
        command = {"type": "item.completed", "item": {
            "type": "command_execution", "command": "fix rate limit parser"}}
        normal = {"type": "turn.completed", "usage": {"output_tokens": 1}}
        output = json.dumps(command) + "\n" + json.dumps(normal)
        self.assertFalse(parse_output_for_quota(output))


class TestRunTask(unittest.TestCase):
    def patch_run(self, fake):
        import agents.adapters.codex as module
        original = module.run_subprocess
        module.run_subprocess = fake
        self.addCleanup(setattr, module, "run_subprocess", original)

    def test_tier_translation_and_task_result(self):
        captured = {}

        def fake(command, cwd, stall_seconds, echo=None):
            captured.update(command=command, cwd=cwd, echo=echo)
            return 0, json.dumps({"type": "turn.completed"}), False

        self.patch_run(fake)
        result = CodexAdapter(make_cfg()).run_task("p", "prime", "high", Path("/p"))
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(captured["command"][captured["command"].index("--model") + 1],
                         "gpt-5.6-sol")
        self.assertEqual(captured["cwd"], Path("/p"))
        self.assertFalse(result.quota_exhausted)

    def test_quota_and_stall_behavior(self):
        quota = json.dumps({"type": "error", "message": "usage limit reached"})
        self.patch_run(lambda *args, **kwargs: (1, quota, False))
        self.assertTrue(CodexAdapter(make_cfg()).run_task(
            "p", "lite", None, Path(".")).quota_exhausted)

    def test_unknown_tier_raises(self):
        with self.assertRaises(AgentsError):
            CodexAdapter(make_cfg(codex_models={"core": "x"})).run_task(
                "p", "prime", None, Path("."))


class TestGetAdapter(unittest.TestCase):
    def test_codex_returns_adapter(self):
        self.assertIsInstance(get_adapter("codex", make_cfg()), CodexAdapter)


if __name__ == "__main__":
    unittest.main()
