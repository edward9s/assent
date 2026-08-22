"""Codex adapter tests; never use the network or a real Codex session."""
import json
import unittest
from pathlib import Path

from assent import AssentError
from assent.adapters import CHECKPOINT_RESUME_RECORD, TaskResult, get_adapter
from assent.adapters.codex import (
    CodexAdapter, build_command, format_stream_event, parse_output_for_billing,
    parse_output_for_quota, parse_output_for_usage,
)
from assent.config import Config, WorkflowActionStep
from tests.engine_support import TEST_MODEL_TIERS

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_cfg(**overrides) -> Config:
    """Build a test Config. Assent ships no model ids, so these tier tables are
    the fixture's own; assertions below name their values directly."""
    values = dict(root=Path("."), assent_dir=Path("./.assent"),
                  tasks_dir=Path("./.assent/plan01"), tasks_name="plan01",
                  workflow_task=(WorkflowActionStep("focused_test"),),
                  adapter_names=("codex",),
                  codex_models=dict(TEST_MODEL_TIERS["codex"]))
    values.update(overrides)
    return Config(**values)


class TestBuildCommand(unittest.TestCase):
    def test_json_model_effort_sandbox_and_stdin_marker(self):
        cmd = build_command(make_cfg(), "the prompt", "gpt-5.6-sol", "max")
        self.assertEqual(cmd[:3], ["codex", "exec", "--json"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "gpt-5.6-sol")
        self.assertIn('model_reasoning_effort="max"', cmd)
        self.assertEqual(
            cmd[-3:], ["--sandbox", "danger-full-access", "-"])
        self.assertNotIn("the prompt", cmd)

    def test_large_prompt_is_not_in_command_arguments(self):
        prompt = "mixed ASCII and Unicode\n�O�W" * 100_000
        cmd = build_command(make_cfg(), prompt, "gpt-5.6-sol", "max")
        self.assertEqual(cmd[-1], "-")
        self.assertNotIn(prompt, cmd)
        self.assertLess(sum(len(part) for part in cmd), 4096)

    def test_effort_can_be_omitted_and_extra_args_are_verbatim(self):
        cfg = make_cfg(codex_command="codex.cmd",
                       codex_extra_args=["--sandbox", "danger-full-access"])
        cmd = build_command(cfg, "p", "custom", None)
        self.assertEqual(cmd[0], "codex.cmd")
        self.assertNotIn("model_reasoning_effort", " ".join(cmd))
        self.assertEqual(cmd[-3:], ["--sandbox", "danger-full-access", "-"])
        self.assertNotIn("--output-schema", cmd)
        self.assertNotIn("--output-last-message", cmd)


class TestFormatStreamEvent(unittest.TestCase):
    def test_real_cli_probe_fixture_contains_ai_message_and_usage(self):
        lines = (FIXTURES / "codex_json_ok.txt").read_text(
            encoding="utf-8").splitlines()
        rendered = [format_stream_event(line) for line in lines]
        self.assertTrue(any(text and "AI| OK" in text for text in rendered))
        self.assertTrue(any(text and "5 tokens" in text for text in rendered))
        self.assertFalse(parse_output_for_quota("\n".join(lines)))

    def test_agent_message_is_displayed(self):
        # The agent_message text is opaque upstream fixture data (Chinese kept on purpose to
        # prove multiline Unicode passthrough); it must render verbatim, never translated.
        event = {"type": "item.completed", "item": {
            "id": "i1", "type": "agent_message", "text": "�Ĥ@��\n�ĤG��"}}
        rendered = format_stream_event(json.dumps(event, ensure_ascii=False))
        self.assertIn("AI| �Ĥ@��", rendered)
        self.assertIn("AI| �ĤG��", rendered)

    def test_tool_and_usage_events_are_displayed(self):
        tool = {"type": "item.started", "item": {
            "type": "command_execution", "command": "python -m unittest",
            "status": "in_progress"}}
        self.assertIn("python -m unittest", format_stream_event(json.dumps(tool)))
        done = {"type": "turn.completed", "usage": {
            "output_tokens": 12, "reasoning_output_tokens": 3}}
        rendered = format_stream_event(json.dumps(done))
        self.assertIn("12", rendered)
        self.assertIn("ended", rendered)

    def test_failure_error_and_non_json_are_displayed(self):
        failed = {"type": "turn.failed", "error": {"message": "boom"}}
        self.assertIn("boom", format_stream_event(json.dumps(failed)))
        self.assertIn("warning", format_stream_event("warning"))
        self.assertIsNone(format_stream_event("  \n"))

    def test_checkpoint_resume_record_is_hidden_from_live_output(self):
        self.assertIsNone(format_stream_event(CHECKPOINT_RESUME_RECORD + "\n"))


class TestUsage(unittest.TestCase):
    def test_fixture_normalizes_supported_categories(self):
        output = (FIXTURES / "codex_json_ok.txt").read_text(encoding="utf-8")
        usage = parse_output_for_usage(output)
        self.assertEqual(len(usage), 1)
        self.assertIsNone(usage[0].provider_model)
        self.assertEqual(usage[0].input_tokens, 12046)
        self.assertEqual(usage[0].cached_input_tokens, 9984)
        self.assertEqual(usage[0].output_tokens, 5)
        self.assertEqual(usage[0].reasoning_output_tokens, 0)

    def test_model_buckets_and_invalid_counters(self):
        event = {"type": "turn.completed", "usage": {"model_usage": {
            "gpt-a": {"input_tokens": 1},
            "gpt-b": {"reasoning_output_tokens": 2, "output_tokens": -1}}}}
        usage = parse_output_for_usage(json.dumps(event))
        self.assertEqual([item.provider_model for item in usage],
                         ["gpt-a", "gpt-b"])
        self.assertIsNone(usage[1].output_tokens)
        self.assertIsNone(parse_output_for_usage(json.dumps({
            "type": "turn.completed", "usage": {"input_tokens": 1.5}})))


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


class TestCheckpointResume(unittest.TestCase):
    def test_exact_final_record_is_recognized(self):
        from assent.adapters import parse_checkpoint_resume_output

        output = "partial\n" + CHECKPOINT_RESUME_RECORD + "\n\n"
        self.assertTrue(parse_checkpoint_resume_output(output, 1, False))

    def test_zero_exit_stall_and_nonfinal_or_lookalike_records_are_rejected(self):
        from assent.adapters import parse_checkpoint_resume_output

        cases = (
            (0, CHECKPOINT_RESUME_RECORD + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD + "\n", True),
            (1, "prefix" + CHECKPOINT_RESUME_RECORD + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD[:-1] + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD + "\ntrailing\n", False),
            (1, CHECKPOINT_RESUME_RECORD + " \n", False),
            (1, '{"type": "assent.checkpoint_resume"}\n', False),
        )
        for exit_code, output, stalled in cases:
            with self.subTest(exit_code=exit_code, output=output, stalled=stalled):
                self.assertFalse(
                    parse_checkpoint_resume_output(output, exit_code, stalled))


class TestBilling(unittest.TestCase):
    def test_error_and_failure_billing_messages_are_detected(self):
        for event in (
            {"type": "error", "message": "Insufficient credit balance"},
            {"type": "turn.failed", "error": {"message": "payment required"}},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "Your balance is too low"}},
        ):
            with self.subTest(event=event):
                self.assertTrue(parse_output_for_billing(json.dumps(event)))
                # billing must not also register as quota (mutually exclusive verdicts)
                self.assertFalse(parse_output_for_quota(json.dumps(event)))

    def test_tool_text_and_normal_completion_are_not_billing(self):
        command = {"type": "item.completed", "item": {
            "type": "command_execution", "command": "audit the credit balance report"}}
        normal = {"type": "turn.completed", "usage": {"output_tokens": 1}}
        output = json.dumps(command) + "\n" + json.dumps(normal)
        self.assertFalse(parse_output_for_billing(output))


class TestRunTask(unittest.TestCase):
    def patch_run(self, fake):
        import assent.adapters.codex as module
        original = module.run_subprocess
        module.run_subprocess = fake
        self.addCleanup(setattr, module, "run_subprocess", original)

    def test_tier_translation_and_task_result(self):
        captured = {}

        def fake(command, cwd, stall_seconds, echo=None, input_text=None):
            captured.update(command=command, cwd=cwd, echo=echo,
                            input_text=input_text)
            return 0, json.dumps({"type": "turn.completed"}), False

        self.patch_run(fake)
        adapter = CodexAdapter(make_cfg())
        requested_model = adapter.resolve("prime")[0]
        prompt = "prompt\n�O�W"
        result = adapter.run_task(prompt, requested_model, "high", Path("/p"))
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(captured["command"][captured["command"].index("--model") + 1],
                         requested_model)
        self.assertEqual(captured["command"][-1], "-")
        self.assertEqual(captured["input_text"], prompt)
        self.assertEqual(captured["cwd"], Path("/p"))
        self.assertTrue(callable(captured["echo"]))
        self.assertFalse(result.quota_exhausted)
        self.assertFalse(result.stalled)

    def test_quota_and_stall_behavior(self):
        quota = json.dumps({"type": "error", "message": "usage limit reached"})
        self.patch_run(lambda *args, **kwargs: (1, quota, False))
        adapter = CodexAdapter(make_cfg())
        self.assertTrue(adapter.run_task(
            "p", adapter.resolve("lite")[0], None, Path(".")).quota_exhausted)

        self.patch_run(lambda *args, **kwargs: (1, quota, True))
        stalled = adapter.run_task(
            "p", adapter.resolve("lite")[0], None, Path("."))
        self.assertTrue(stalled.stalled)
        self.assertFalse(stalled.quota_exhausted)

    def test_quota_like_assistant_prose_on_success_is_not_quota(self):
        for phrase in ("quota exceeded", "rate limit", "session limit"):
            with self.subTest(phrase=phrase):
                line = json.dumps({"type": "item.completed", "item": {
                    "type": "agent_message",
                    "text": f"The answer discusses {phrase}."}})
                self.patch_run(lambda *args, **kwargs: (0, line + "\n", False))
                result = CodexAdapter(make_cfg()).run_task(
                    "p", "gpt-5.6-sol", None, Path("."))
                self.assertEqual(result.exit_code, 0)
                self.assertFalse(result.quota_exhausted)

    def test_billing_output_sets_failure_kind_not_quota(self):
        billing = json.dumps({"type": "turn.failed",
                              "error": {"message": "Credit balance is too low"}})
        self.patch_run(lambda *args, **kwargs: (1, billing, False))
        adapter = CodexAdapter(make_cfg())
        result = adapter.run_task(
            "p", adapter.resolve("lite")[0], None, Path("."))
        self.assertFalse(result.quota_exhausted)
        self.assertEqual(result.failure_kind, "billing")

        # a stall carrying billing-looking text is still a stall, never billing
        self.patch_run(lambda *args, **kwargs: (1, billing, True))
        stalled = adapter.run_task(
            "p", adapter.resolve("lite")[0], None, Path("."))
        self.assertTrue(stalled.stalled)
        self.assertIsNone(stalled.failure_kind)

    def test_authentication_output_sets_failure_kind_not_quota(self):
        output = json.dumps({
            "type": "turn.failed",
            "error": {"message": "Not logged in. Please run codex login."},
        })
        self.patch_run(lambda *args, **kwargs: (1, output, False))

        result = CodexAdapter(make_cfg()).run_task(
            "p", "gpt-5.6-sol", "medium", Path("."))

        self.assertEqual(result.failure_kind, "authentication")
        self.assertFalse(result.quota_exhausted)

    def test_checkpoint_resume_record_sets_distinct_result_and_keeps_raw_output(self):
        output = "partial\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = CodexAdapter(make_cfg()).run_task(
            "p", "gpt-5.6-sol", "medium", Path("."))
        self.assertTrue(result.checkpoint_resume)
        self.assertFalse(result.quota_exhausted)
        self.assertEqual(result.output, output)
        self.assertIsNone(result.failure_kind)

    def test_stream_agent_message_preserves_checkpoint_resume_control(self):
        output = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": CHECKPOINT_RESUME_RECORD}}) + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = CodexAdapter(make_cfg()).run_task(
            "p", "gpt-5.6-sol", "medium", Path("."))
        self.assertTrue(result.checkpoint_resume)
        self.assertFalse(result.quota_exhausted)
        self.assertIsNone(result.failure_kind)

    def test_quota_and_control_record_use_the_quota_path(self):
        quota = json.dumps({"type": "error", "message": "usage limit reached"})
        output = quota + "\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = CodexAdapter(make_cfg()).run_task(
            "p", "gpt-5.6-sol", "medium", Path("."))
        self.assertTrue(result.quota_exhausted)
        self.assertFalse(result.checkpoint_resume)

    def test_terminal_record_overrides_preceding_billing_prose(self):
        prose = json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "I checked the credit balance report."}})
        output = prose + "\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = CodexAdapter(make_cfg()).run_task(
            "p", "gpt-5.6-sol", "medium", Path("."))
        self.assertTrue(result.checkpoint_resume)
        self.assertFalse(result.quota_exhausted)
        self.assertIsNone(result.failure_kind)



    def test_unknown_tier_raises(self):
        with self.assertRaises(AssentError):
            CodexAdapter(make_cfg(codex_models={"core": "x/high"})).resolve(
                "prime")


class TestGetAdapter(unittest.TestCase):
    def test_codex_returns_adapter(self):
        self.assertIsInstance(get_adapter("codex", make_cfg()), CodexAdapter)


if __name__ == "__main__":
    unittest.main()
