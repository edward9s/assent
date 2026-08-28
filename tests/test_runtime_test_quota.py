"""Persistent quota behavior for runtime repair roles."""
import contextlib
import io
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

from assent import engine
from assent.adapters import TaskResult
from assent.plan import read_runtime_test_workflow_state
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result
from tests.test_runtime_test_action import python_command


class Clock:
    def __init__(self):
        self.value = datetime(2035, 1, 2, 3, 4, tzinfo=timezone.utc)
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class RuntimeTestQuotaTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.write_task(1, status="DONE")
        (self.root / "src").mkdir()
        (self.root / "src" / "value.txt").write_text(
            "bad\n", encoding="utf-8")
        self.counter = self.root.parent / f"{self.root.name}-runtime-count.txt"
        self.addCleanup(self.counter.unlink, missing_ok=True)
        self.command = python_command(
            "from pathlib import Path; import sys; "
            f"p=Path({str(self.counter)!r}); "
            "p.write_text((p.read_text() if p.exists() else '')+'x'); "
            "sys.exit(0 if Path('src/value.txt').read_text().strip() == "
            "'good' else 4)")

    def build_runtime(self, adapters=("claude",), poll=30):
        adapter_value = (f'"{adapters[0]}"' if len(adapters) == 1 else
                         "[" + ", ".join(f'\"{name}\"' for name in adapters) + "]")
        cfg = self.build(extra_config=(
            "[run]\n"
            f"quota_poll_minutes = {poll}\n"
            "[abilities.repair]\n"
            'prompt = "Repair the runtime failure."\n'
            "writes = true\n"
            "[roles.writer]\n"
            'ability = ["repair"]\n'
            "[workflow]\n"
            'task = [{ action = "focused_test" }]\n'
            "runtime_test = [{ action = \"runtime_test\" }, "
            f"{{ role = \"writer\", adapter = {adapter_value}, model = \"lite\" }}, "
            "{ action = \"runtime_test\" }]\n"))
        (self.plan_dir / "_runtime_test.toml").write_text(
            f'execution = "explicit"\ncommand = {json.dumps(self.command)}\n',
            encoding="utf-8")
        self.commit_all()
        return cfg

    def repair(self, _prompt):
        (self.execution_root() / "src" / "value.txt").write_text(
            "good\n", encoding="utf-8")
        return ok_result()

    def run_runtime(self, cfg, primary, clock, alternates=None, sleep=None):
        alternates = alternates or {}
        with mock.patch.object(engine.contracts, "require_contracts"), \
                mock.patch("assent.engine.get_adapter",
                           side_effect=lambda name, _cfg: alternates[name]), \
                contextlib.redirect_stdout(io.StringIO()):
            return engine.run_runtime_test(
                cfg, adapter=primary, sleep=sleep or clock.sleep,
                now=clock.now)

    def quota(self, reset_at=None, *, checkpoint=False):
        return TaskResult(
            1, "quota exhausted", True, reset_at,
            failure_kind="quota", checkpoint_resume=checkpoint)

    def test_known_five_hour_wait_outranks_checkpoint_and_clears_on_completion(self):
        cfg = self.build_runtime()
        clock = Clock()
        adapter = ScriptedAdapter([
            self.quota(clock.now() + timedelta(hours=5), checkpoint=True),
            self.repair,
        ])

        self.assertEqual(self.run_runtime(cfg, adapter, clock), 0)

        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(sum(clock.sleeps), 5 * 60 * 60 + 2 * 60)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "xx")
        self.assertEqual(
            read_runtime_test_workflow_state(self.plan_dir).quota_waits, ())

    def test_quota_rotates_only_to_the_next_configured_adapter(self):
        cfg = self.build_runtime(("claude", "codex"))
        clock = Clock()
        primary = ScriptedAdapter([
            self.quota(clock.now() + timedelta(hours=5))])
        alternate = ScriptedAdapter([self.repair])

        self.assertEqual(self.run_runtime(
            cfg, primary, clock, {"codex": alternate}), 0)

        self.assertEqual((len(primary.calls), len(alternate.calls)), (1, 1))
        self.assertEqual(clock.sleeps, [])

    def test_all_limited_uses_earliest_known_reset_even_with_unknown_wait(self):
        cfg = self.build_runtime(("claude", "codex"), poll=30)
        clock = Clock()
        primary = ScriptedAdapter([self.quota()])
        alternate = ScriptedAdapter([
            self.quota(clock.now() + timedelta(minutes=8)), self.repair])

        self.assertEqual(self.run_runtime(
            cfg, primary, clock, {"codex": alternate}), 0)

        self.assertEqual(sum(clock.sleeps), 10 * 60)
        self.assertEqual((len(primary.calls), len(alternate.calls)), (1, 2))

    def test_unknown_reset_uses_configured_low_frequency_poll(self):
        cfg = self.build_runtime(poll=7)
        clock = Clock()
        adapter = ScriptedAdapter([self.quota(), self.repair])

        self.assertEqual(self.run_runtime(cfg, adapter, clock), 0)

        self.assertEqual(sum(clock.sleeps), 7 * 60)
        self.assertEqual(len(adapter.calls), 2)

    def test_interrupt_and_restart_preserve_wait_and_do_not_rerun_action(self):
        cfg = self.build_runtime()
        clock = Clock()
        reset = clock.now() + timedelta(hours=5)

        def quota_with_progress(_prompt):
            (self.execution_root() / "src" / "progress.txt").write_text(
                "kept\n", encoding="utf-8")
            return self.quota(reset)

        first = ScriptedAdapter([quota_with_progress])

        def interrupt(_seconds):
            raise KeyboardInterrupt

        self.assertEqual(self.run_runtime(
            cfg, first, clock, sleep=interrupt), 130)
        state = read_runtime_test_workflow_state(self.plan_dir)
        self.assertEqual(state.quota_waits[0].reset_at, reset)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "x")
        self.assertEqual(
            (self.execution_root() / "src" / "progress.txt").read_text(
                encoding="utf-8"), "kept\n")

        resumed = ScriptedAdapter([self.repair])

        def advance_without_early_call(seconds):
            self.assertEqual(resumed.calls, [])
            clock.sleep(seconds)

        self.assertEqual(self.run_runtime(
            cfg, resumed, clock, sleep=advance_without_early_call), 0)
        self.assertEqual(len(resumed.calls), 1)
        self.assertEqual(self.counter.read_text(encoding="utf-8"), "xx")


if __name__ == "__main__":
    import unittest
    unittest.main()
