"""engine tests for one session round: the adapter call, its process outcome, and the
quota/billing interruptions that keep the task resumable.

Every case here starts (or refuses to start) a scripted AI session and checks what the
scheduler records about it -- the resolved identity in the prompt and journal, an adapter
exit or watchdog stall, a quota round with its wait math, and an exhausted prepaid balance.
The core run loop lives in tests.test_engine; shared fixtures in tests.engine_support.

Chinese literals that remain are deliberate user/upstream passthrough data."""
import _thread
import contextlib
import io
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from assent import AssentError, engine, gitops
from assent.adapters import CHECKPOINT_RESUME_RECORD, TaskResult
from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     wake_stop_waiters)
from assent.config import load_config
from assent.plan import append_entry, journal_path_for, parse_task_file, set_status
from tests.engine_support import (EngineTestCase, ScriptedAdapter, ok_result,
                                  task_text)
from tests.test_contracts import GlobalContractsMixin


class TestAntigravitySession(GlobalContractsMixin, EngineTestCase):
    def setUp(self):
        super().setUp()
        from assent.adapters import antigravity
        self.antigravity = antigravity
        self.catalog = antigravity.parse_models_catalog(
            (Path(__file__).resolve().parent / "fixtures"
             / "agy_models_1.1.5.txt").read_text(encoding="utf-8"))

    def adapter(self, cfg):
        from assent.adapters.antigravity import AntigravityAdapter
        return AntigravityAdapter(cfg, catalog=self.catalog)

    def patch_session(self, fake):
        patch = mock.patch.object(self.antigravity, "run_subprocess", fake)
        patch.start()
        self.addCleanup(patch.stop)

    def test_legacy_instructions_example_does_not_break_the_resolved_identity(self):
        # The instructions file of an older project still shows only codex/claude. The
        # run-specific prompt, the closeout the agent writes, and the journal validator must
        # all still agree on antigravity, so the task passes on the first attempt.
        path = self.write_task(1, model="lite")
        (self.root / ".assent" / "instructions.md").write_text(
            "工作指示\n\n日誌 by 欄位範例:by = \"codex\" 或 \"claude\"\n",
            encoding="utf-8")
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter]\nname = "antigravity"\n', encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        commands: list[list[str]] = []

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None):
            commands.append(command)
            (Path(cwd) / "src").mkdir(exist_ok=True)
            (Path(cwd) / "src" / "done.py").write_text("ok", encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="antigravity",
                         requested_model=command[command.index("--model") + 1],
                         requested_effort=command[command.index("--effort") + 1],
                         event="done", summary="完成")
            return 0, "done\n", False

        self.patch_session(fake)
        adapter = self.adapter(cfg)
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(commands), 1)      # first attempt accepted, no retry loop
        prompt = commands[0][commands[0].index("--print") + 1]
        self.assertIn('by = "antigravity"', prompt)
        self.assertIn('requested_model = "gemini-3.5-flash"', prompt)
        self.assertIn('requested_effort = "medium"', prompt)
        self.assertIn("authoritative for this run's journal entry", prompt)
        self.assertEqual(parse_task_file(path).status, "DONE")

        from assent.plan import read_entries
        done = next(e for e in read_entries(journal_path_for(path))
                    if e["by"] == "antigravity")
        # the journal identity is the actual flag pair, not the abstract tier
        self.assertEqual(done["requested_model"], "gemini-3.5-flash")
        self.assertEqual(done["requested_effort"], "medium")
        self.assertTrue(any(s.startswith("auto(plan01/t001): ")
                            for s in self.subjects()))

    def test_adapter_classification_reaches_the_reason_and_the_journal(self):
        path = self.write_task(1, model="lite")
        (self.root / ".assent" / "assent.toml").write_text(
            '[run]\nretry_per_task = 0\n[adapter]\nname = "antigravity"\n',
            encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        self.patch_session(lambda *args, **kwargs: (
            1, "Error: permission denied for tool write_to_file", False))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True,
                                        adapter=self.adapter(cfg)), 0)

        self.assertIn("classified by the adapter as permission", out.getvalue())
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        failure = next(e for e in entries if e["event"] == "adapter_exit")
        self.assertEqual(failure["failure_kind"], "permission")
        self.assertEqual(failure["exit_code"], 1)
        self.assertEqual(failure["agent"], "antigravity")
        # a classification never turns a non-zero exit into an accepted task
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_quota_interrupt_then_resume_keeps_identity_everywhere(self):
        """A quota round and its resume must resolve to the exact same requested_model /
        requested_effort in the prompt, the CLI command, the terminal output, and both the
        scheduler's quota journal entry and the execution AI's own done entry."""
        path = self.write_task(1, model="prime", effort="slight")
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter]\nname = "antigravity"\n', encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        sleeps: list[float] = []
        calls: list[list[str]] = []

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None):
            calls.append(command)
            if len(calls) == 1:
                return (1, "Error: Resource has been exhausted (e.g. check quota).", False)
            (Path(cwd) / "src").mkdir(exist_ok=True)
            (Path(cwd) / "src" / "done.py").write_text("ok", encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="antigravity",
                         requested_model=command[command.index("--model") + 1],
                         requested_effort=command[command.index("--effort") + 1],
                         event="done", summary="完成 (resumed)")
            return (0, "done\n", False)

        self.patch_session(fake)
        adapter = self.adapter(cfg)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=adapter,
                sleep=sleeps.append, now=lambda: t0), 0)

        self.assertEqual(len(calls), 2)     # quota round, then one resumed round
        terminal = out.getvalue()
        for command in calls:
            self.assertEqual(command[command.index("--model") + 1], "gemini-3.1-pro")
            self.assertEqual(command[command.index("--effort") + 1], "low")
        self.assertIn("gemini-3.1-pro", terminal)

        resume_prompt = calls[1][calls[1].index("--print") + 1]
        self.assertIn("resume", resume_prompt.lower())
        self.assertIn('requested_model = "gemini-3.1-pro"', resume_prompt)
        self.assertIn('requested_effort = "low"', resume_prompt)

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota_entry = next(e for e in entries if e["event"] == "quota")
        done_entry = next(e for e in entries if e["event"] == "done")
        self.assertEqual(quota_entry["requested_model"], "gemini-3.1-pro")
        self.assertEqual(quota_entry["requested_effort"], "low")
        self.assertEqual(done_entry["requested_model"], "gemini-3.1-pro")
        self.assertEqual(done_entry["requested_effort"], "low")
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestAdapterProcessOutcomes(GlobalContractsMixin, EngineTestCase):
    def test_nonzero_done_retries_without_done_checkpoint_then_succeeds(self):
        path = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        def failed_done(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "kept.py").write_text("work", encoding="utf-8")
            set_status(path, "DONE")
            output = ("transport failed token=TOPSECRET " + "x" * 400
                      + " useful-tail")
            return TaskResult(exit_code=7, output=output,
                              quota_exhausted=False, reset_at=None)

        def successful_retry(prompt):
            self.assertFalse(any(
                subject.startswith("auto(plan01/t001): ")
                for subject in self.subjects()))
            self.assertIn("exit code 7", prompt)
            self.assertIn("useful-tail", prompt)
            self.assertNotIn("TOPSECRET", prompt)
            return ok_result()

        adapter = ScriptedAdapter([failed_done, successful_retry])
        with mock.patch.object(engine, "_run_verify", return_value=0) as verify:
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=adapter), 0)
        verify.assert_called_once()
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertIn("src/kept.py", self._git_execution("ls-files"))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        failed = next(e for e in entries if e["event"] == "adapter_exit")
        self.assertEqual(failed["exit_code"], 7)
        self.assertFalse(failed["stalled"])
        self.assertEqual(failed["agent"], "claude")
        self.assertEqual(failed["requested_model"], "lite")
        self.assertEqual(failed["requested_effort"], "medium")
        self.assertNotIn("TOPSECRET", failed["summary"])
        self.assertLess(len(failed["summary"]), 400)

    def test_nonzero_todo_exhausts_retries_and_keeps_work(self):
        path = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        def first(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(exit_code=2, output="first transport failure",
                              quota_exhausted=False, reset_at=None)

        adapter = ScriptedAdapter([
            first,
            TaskResult(exit_code=4, output="second transport failure",
                       quota_exhausted=False, reset_at=None),
        ])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertIn("exit code 2", adapter.calls[1][0])
        self.assertIn("src/partial.py", self._git_execution("ls-files"))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        failures = [e for e in entries if e["event"] == "adapter_exit"]
        self.assertEqual([e["exit_code"] for e in failures], [2, 4])
        blocked = next(e for e in entries if e["event"] == "blocked")
        self.assertIn("exit code 4", blocked["summary"])

    def test_nonzero_self_blocked_retries_then_scheduler_blocks(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def blocked(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text(
                "preserved", encoding="utf-8")
            set_status(path, "BLOCKED")
            return TaskResult(exit_code=9, output="dependency unavailable",
                              quota_exhausted=False, reset_at=None)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=ScriptedAdapter([blocked]))
        self.assertEqual(rc, 0)
        self.assertIn("exit code 9", out.getvalue())
        self.assertFalse(any(
            subject == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for subject in self.subjects()))

        from assent.plan import read_entries
        failure = next(e for e in read_entries(journal_path_for(path))
                       if e["event"] == "adapter_exit")
        self.assertEqual(failure["exit_code"], 9)
        scheduler_blocked = next(e for e in read_entries(journal_path_for(path))
                                 if e["by"] == "scheduler"
                                 and e["event"] == "blocked")
        self.assertIn("exit code 9", scheduler_blocked["summary"])

    def test_nonzero_self_blocked_with_scope_violation_fails_closed(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def unsafe_blocked(prompt):
            (self.execution_root() / "rogue.py").write_text(
                "unsafe", encoding="utf-8")
            path.write_text(
                task_text(status="BLOCKED", scope=("src/", "rogue.py"),
                          verify="echo unsafe"),
                encoding="utf-8", newline="\n")
            return TaskResult(exit_code=8, output="adapter failed",
                              quota_exhausted=False, reset_at=None)

        self.run_quiet(cfg, once=True,
                       adapter=ScriptedAdapter([unsafe_blocked]))
        self.assertFalse(any(
            subject == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for subject in self.subjects()))
        from assent.plan import read_entries
        blocked = next(e for e in read_entries(journal_path_for(path))
                       if e["event"] == "blocked")
        self.assertIn("exit code 8", blocked["summary"])
        self.assertIn("fields other than status", blocked["summary"])
        self.assertIn("outside scope", blocked["summary"])

    def test_watchdog_stall_has_distinct_non_quota_event(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()
        stalled = TaskResult(exit_code=1, output="rate limit exceeded",
                             quota_exhausted=False, reset_at=None, stalled=True)
        self.run_quiet(cfg, once=True, adapter=ScriptedAdapter([stalled]))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertIn("adapter_stall", [e["event"] for e in entries])
        self.assertNotIn("quota", [e["event"] for e in entries])


class TestBillingAbort(GlobalContractsMixin, EngineTestCase):
    """A billing/insufficient-balance failure aborts the whole run without a retry.

    Dispatch is purely on failure_kind="billing" (never an adapter name), so this uses a
    plain ScriptedAdapter result, exactly how a fourth adapter would opt in."""

    def billing_step(self, path):
        def step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(
                exit_code=1, output="Credit balance is too low",
                quota_exhausted=False, reset_at=None, failure_kind="billing")
        return step

    def test_billing_consumes_no_retry_and_aborts_before_next_task(self):
        p1 = self.write_task(1)
        p2 = self.write_task(2)
        cfg = self.build(retry=2)          # retries available, but none may be spent
        self.commit_all()

        # only one step: a second call (a retry, or advancing to t002) would raise
        adapter = ScriptedAdapter([self.billing_step(p1)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, adapter=adapter)

        self.assertEqual(rc, 1)
        self.assertEqual(len(adapter.calls), 1)             # no retry, no advance to t002
        text = out.getvalue()
        self.assertIn("billing/balance", text)
        self.assertIn("Top up the account", text)

        # the failing task stays resumable (WIP), the next task is untouched (TODO)
        self.assertEqual(parse_task_file(p1).status, "WIP")
        self.assertEqual(parse_task_file(p2).status, "TODO")

        # progress kept in a wip checkpoint; no BLOCKED checkpoint was written
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ") for s in subjects))
        self.assertFalse(any("BLOCKED" in s for s in subjects))
        self.assertIn("src/partial.py", self._git_execution("ls-files"))

        # a distinct billing journal entry, separate from a normal acceptance failure
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(p1))
        billing = next(e for e in entries
                       if e["by"] == "scheduler" and e["event"] == "billing")
        self.assertIn("top-up", billing["summary"].lower())
        self.assertEqual(billing["agent"], "claude")
        self.assertEqual(billing["requested_model"], "lite")
        self.assertNotIn("blocked", [e["event"] for e in entries])

    def test_billing_task_resumes_cleanly_on_a_later_run(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([self.billing_step(path)])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")

        # after a top-up, the same task is picked up again and resumes with a continue prompt
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestQuotaAndResume(GlobalContractsMixin, EngineTestCase):
    def rotation_config(self):
        cfg = self.build()
        cfg.adapter_names = ("claude", "codex")
        return cfg

    def test_quota_waits_then_resumes_same_task(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        reset = t0 + timedelta(minutes=5)
        sleeps: list[float] = []

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("p", encoding="utf-8")
            # The adapter can write DONE before the process result is classified as quota;
            # the scheduler must put the task back into the resumable state first.
            set_status(path, "DONE")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=reset)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            return self.ai_done(path)(prompt)

        adapter = ScriptedAdapter([quota_step, resumed])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), (5 + 2) * 60, delta=1)  # +2 minute buffer
        self.assertIn("resume", adapter.calls[1][0])
        self.assertIn("Waiting for quota reset before resuming", out.getvalue())
        # one zero-token capability preflight before the run, then one per attempt
        self.assertEqual(adapter.resolve_calls, ["lite", "lite", "lite"])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in subjects))
        self.assertEqual(
            len([s for s in subjects if s.startswith("auto(plan01/t001): ")]), 1)
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "lite")
        self.assertEqual(quota["requested_effort"], "medium")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, waiting for quota reset before resuming")
        self.assertNotIn("session", [e["event"] for e in entries])

    def test_unknown_quota_wait_names_poll_and_preserves_resume_progress(self):
        path = self.write_task(1)
        cfg = self.build()
        cfg.quota_poll_minutes = 7
        self.commit_all()

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            set_status(path, "DONE")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            self.assertIn("resume", prompt.lower())
            return self.ai_done(path)(prompt)

        adapter = ScriptedAdapter([quota_step, resumed])
        sleeps: list[float] = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append)

        self.assertEqual(rc, 0)
        self.assertEqual(sum(sleeps), 7 * 60)
        self.assertEqual(parse_task_file(path).status, "DONE")
        terminal = out.getvalue()
        self.assertIn(
            "Waiting for quota poll (every 7 minutes) before resuming", terminal)
        self.assertIn("Quota poll (every 7 minutes)", terminal)
        self.assertNotIn("reset", terminal.lower())
        self.assertIn("src/partial.py", self._git_execution("ls-files"))
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in self.subjects()))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, waiting for quota poll "
            "(every 7 minutes) before resuming")
        self.assertNotIn("reset", quota["summary"].lower())

    def test_checkpoint_resume_keeps_same_adapter_without_wait_rotation_or_retry(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        cfg.adapter_names = ("claude", "codex")
        self.commit_all()
        control = TaskResult(
            exit_code=1, output=CHECKPOINT_RESUME_RECORD + "\n",
            quota_exhausted=False, reset_at=None, checkpoint_resume=True)

        def checkpoint_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            set_status(path, "DONE")
            return control

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            return self.ai_done(path)(prompt)

        claude = ScriptedAdapter(
            [checkpoint_step, resumed], resolved_model="claude-lite")
        codex = ScriptedAdapter([], resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            with mock.patch("assent.engine.get_adapter", return_value=codex):
                result = engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append)

        self.assertEqual(result, 0)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(codex.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(claude.calls[0][1:], claude.calls[1][1:])
        self.assertIn("resume", claude.calls[1][0].lower())

        terminal = out.getvalue()
        self.assertIn("Checkpoint-resume control received", terminal)
        self.assertIn("same adapter command", terminal)
        self.assertNotIn("Waiting for quota reset", terminal)
        self.assertNotIn("Switching adapter", terminal)

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        checkpoint = next(entry for entry in entries
                          if entry["event"] == "checkpoint_resume")
        self.assertEqual(checkpoint["agent"], "claude")
        self.assertEqual(checkpoint["requested_model"], "claude-lite")
        self.assertEqual(checkpoint["requested_effort"], "medium")
        self.assertIn(CHECKPOINT_RESUME_RECORD, checkpoint["detail"])
        self.assertNotIn("quota", [entry["event"] for entry in entries])
        self.assertTrue(any(subject.startswith("wip(plan01/t001): ")
                            for subject in self.subjects()))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_quota_preserves_an_explicit_blocked_result(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def blocked_quota(prompt):
            set_status(path, "BLOCKED")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "BLOCKED")
            return ok_result()

        self.assertEqual(
            engine.run(cfg, once=True, sleep=lambda _: None,
                       adapter=ScriptedAdapter([blocked_quota, resumed])), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertFalse(any(s.startswith("auto(plan01/t001): ")
                             for s in self.subjects()))

    def test_quota_rotates_to_next_adapter_and_records_each_identity(self):
        path = self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(
                exit_code=1, output="", quota_exhausted=True, reset_at=None)

        claude = ScriptedAdapter([quota_step], resolved_model="claude-lite")
        codex = ScriptedAdapter(
            [self.ai_done(
                path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            with mock.patch("assent.engine.get_adapter", return_value=codex):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append), 0)

        self.assertEqual(sleeps, [])
        self.assertIn("resume", codex.calls[0][0])
        terminal = out.getvalue()
        self.assertIn("Switching adapter claude -> codex immediately", terminal)
        self.assertNotIn("waiting for reset", terminal.lower())
        self.assertNotIn("rotation poll", terminal.lower())
        self.assertTrue(any(
            subject.startswith("wip(plan01/t001): ")
            for subject in self.subjects()))
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(entry for entry in entries if entry["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "claude-lite")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, switching immediately to adapter codex")
        self.assertNotIn("wait", quota["summary"].lower())
        done = next(entry for entry in entries if entry["by"] == "codex")
        self.assertEqual(done["requested_model"], "codex-lite")

    def test_rotation_resolves_each_adapter_effort_and_names_it_in_the_line(self):
        # Each adapter resolves its own built-in lite default independently
        # (claude normal -> medium, codex slight -> low), and the opening line says
        # which one is running.
        path = self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()

        def quota_step(prompt):
            return TaskResult(
                exit_code=1, output="", quota_exhausted=True, reset_at=None)

        claude = ScriptedAdapter([quota_step], resolved_model="claude-lite")
        codex = ScriptedAdapter(
            [self.ai_done(path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=lambda _: None), 0)

        self.assertEqual(claude.calls[0][2], "medium")
        self.assertEqual(codex.calls[0][2], "low")
        self.assertEqual(
            [line for line in out.getvalue().splitlines() if "Session:" in line],
            ["  Session: claude | lite->claude-lite | normal->medium",
             "  Session: codex | lite->codex-lite | slight->low"])

    def test_complete_quota_rotation_waits_then_continues_from_next_adapter(self):
        path = self.write_task(1)
        cfg = self.rotation_config()
        cfg.rotation_poll_minutes = 2
        self.commit_all()
        quota = TaskResult(
            exit_code=1, output="", quota_exhausted=True, reset_at=None)
        claude = ScriptedAdapter(
            [quota, self.ai_done(path)], resolved_model="claude-lite")
        codex = ScriptedAdapter([quota], resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                result = engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append)

        self.assertEqual(result, 0)
        self.assertEqual(sum(sleeps), 2 * 60)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(len(codex.calls), 1)
        self.assertIn("Every adapter in the rotation is quota-exhausted",
                      out.getvalue())
        self.assertIn("continuing with claude", out.getvalue())
        self.assertEqual(out.getvalue().count(
            "Every adapter in the rotation is quota-exhausted"), 1)

        from assent.plan import read_entries
        quotas = [entry for entry in read_entries(journal_path_for(path))
                  if entry["event"] == "quota"]
        self.assertEqual(len(quotas), 2)
        self.assertEqual(
            [(entry["agent"], entry["requested_model"])
             for entry in quotas],
            [("claude", "claude-lite"), ("codex", "codex-lite")])
        self.assertEqual(
            quotas[0]["summary"],
            "Quota exhausted; progress kept, switching immediately to adapter codex")
        self.assertEqual(
            quotas[1]["summary"],
            "Quota exhausted; progress kept, every adapter in the rotation is "
            "quota-exhausted; waiting for rotation poll before continuing with claude")

    def test_all_rotation_adapters_are_preflighted_before_worktree_creation(self):
        self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()
        claude = ScriptedAdapter([ok_result()])
        codex = ScriptedAdapter([])
        codex.preflight = mock.Mock(return_value=["unsupported invocation"])
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                result = engine.run(cfg, once=True, adapter=claude)

        self.assertEqual(result, 1)
        self.assertEqual(claude.calls, [])
        self.assertEqual(codex.calls, [])
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())
        codex.preflight.assert_called_once()
        self.assertIn("codex capability preflight: FAIL", out.getvalue())

    def test_wip_task_resumed_on_startup(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestInterruptedTaskResume(GlobalContractsMixin, EngineTestCase):
    def test_keyboard_interrupt_marks_unverified_done_wip_then_resumes(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def interrupted(prompt):
            set_status(path, "DONE")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([interrupted])), 130)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        interrupt = next(e for e in entries
                         if e["by"] == "scheduler"
                         and e["event"] == "interrupt"
                         and "User interrupt" in e["summary"])
        self.assertEqual(interrupt["agent"], "claude")
        self.assertEqual(interrupt["requested_model"], "lite")
        self.assertEqual(interrupt["requested_effort"], "medium")

        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_interrupt_during_post_auto_report_keeps_done_without_duplicate_auto(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        refreshes = 0

        def interrupt_first_report(cfg):
            nonlocal refreshes
            refreshes += 1
            if refreshes == 1:
                raise KeyboardInterrupt

        with mock.patch.object(engine, "try_write_report",
                               side_effect=interrupt_first_report):
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=ScriptedAdapter([
                    self.ai_done(path, {"src/done.py": "done"})])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        autos = [s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]
        self.assertEqual(len(autos), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        # A later run sees the terminal task as already closed and cannot synthesize another
        # auto marker from the report-refresh interruption.
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_interrupt_after_dirty_terminal_auto_commit_keeps_done(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        real_commit_if_dirty = engine.gitops.commit_if_dirty

        def commit_then_interrupt(root, message, excludes=()):
            committed = real_commit_if_dirty(root, message, excludes)
            if message.startswith("auto(plan01/t001): "):
                raise KeyboardInterrupt
            return committed

        with mock.patch.object(engine.gitops, "commit_if_dirty",
                               side_effect=commit_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=ScriptedAdapter([
                    self.ai_done(path, {"src/done.py": "done"})])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(entry["event"] == "interrupt" for entry in entries))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_interrupt_after_resumed_empty_terminal_auto_commit_keeps_done(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        real_commit_empty = engine.gitops.commit_empty

        def empty_commit_then_interrupt(root, message):
            real_commit_empty(root, message)
            if message.startswith("auto(plan01/t001): "):
                raise KeyboardInterrupt

        with mock.patch.object(engine.gitops, "commit_empty",
                               side_effect=empty_commit_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([
                    self.ai_done(path)])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(entry["event"] == "interrupt" for entry in entries))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_matching_auto_commit_before_terminal_closeout_cannot_recover_done(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        expected = "auto(plan01/t001): task"

        def interrupted(prompt):
            root = self.execution_root()
            (root / "src" / "before_closeout.py").parent.mkdir(exist_ok=True)
            (root / "src" / "before_closeout.py").write_text(
                "work", encoding="utf-8")
            # This terminal-looking commit belongs to the adapter phase, before the scheduler
            # has passed _evaluate and armed its closeout witness.
            gitops.commit_empty(root, expected)
            set_status(path, "DONE")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([interrupted])), 130)

        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(entry["event"] == "interrupt" for entry in entries))
        self.assertIn(expected, self.subjects())
        self.assertTrue(any(
            subject.startswith("wip(plan01/t001): ")
            for subject in self.subjects()))

    def test_assent_error_marks_current_task_wip_and_keeps_exit_code(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def failed(prompt):
            raise AssentError("連線中斷")

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([failed])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(e["event"] == "interrupt"
                            and "infrastructure error" in e["summary"]
                            for e in entries))

    def test_os_error_marks_current_task_wip_as_infrastructure_failure(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def failed(prompt):
            raise OSError("adapter executable unavailable")

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([failed])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        interrupt = next(e for e in read_entries(journal_path_for(path))
                         if e["event"] == "interrupt")
        self.assertIn("infrastructure error", interrupt["summary"])

    def test_interrupted_self_blocked_task_is_not_overwritten(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def blocked_then_interrupted(prompt):
            set_status(path, "BLOCKED")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True,
            adapter=ScriptedAdapter([blocked_then_interrupted])), 130)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_quota_wait_interrupt_marks_task_wip(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        quota = TaskResult(exit_code=1, output="", quota_exhausted=True,
                           reset_at=None)

        def interrupt_sleep(seconds):
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([quota]),
            sleep=interrupt_sleep), 130)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        events = [e["event"] for e in read_entries(journal_path_for(path))]
        self.assertIn("quota", events)
        self.assertIn("interrupt", events)

    def test_stop_wake_during_quota_wait_reaches_the_interrupt_cleanup(self):
        """The wake is only a wake: it releases the sleeping main thread so the
        stdin watcher's already-pending KeyboardInterrupt is delivered, and the
        ordinary interrupt cleanup then runs unchanged."""
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        quota = TaskResult(exit_code=1, output="", quota_exhausted=True,
                           reset_at=None)
        parked = threading.Event()

        def stop_the_run() -> None:
            """Exactly what the stdin stop watcher does on EOF, in that order:
            mark the interrupt, then release the wait it is stuck behind."""
            parked.wait(30)
            _thread.interrupt_main()
            wake_stop_waiters()

        waker = threading.Thread(target=stop_the_run, daemon=True)
        self.addCleanup(waker.join, 30)
        self.addCleanup(clear_stop_wake)

        def sleep(seconds):
            # The production wait, entered on a segment of the full length: a
            # pending KeyboardInterrupt alone would sit here for 60 seconds.
            self.assertEqual(seconds, engine._COUNTDOWN_SEGMENT)
            parked.set()
            interruptible_sleep(seconds)

        waker.start()
        started = time.monotonic()
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([quota]),
            sleep=sleep), 130)
        self.assertLess(time.monotonic() - started, engine._COUNTDOWN_SEGMENT)

        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        events = [e["event"] for e in read_entries(journal_path_for(path))]
        self.assertIn("quota", events)
        self.assertIn("interrupt", events)

    def test_idle_interrupt_does_not_mark_any_task(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        real_parse = engine.Plan.parse
        calls = 0

        def parse_then_interrupt(plan_dir):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return real_parse(plan_dir)

        with mock.patch.object(engine.Plan, "parse",
                               side_effect=parse_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([])), 130)
        self.assertEqual(parse_task_file(path).status, "TODO")
        self.assertFalse(journal_path_for(path).exists())


class TestQuotaMath(GlobalContractsMixin, EngineTestCase):
    def test_quota_wait_seconds(self):
        cfg = self.build()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        future = t0 + timedelta(minutes=10)
        self.assertAlmostEqual(
            engine._quota_wait_seconds(cfg, future, lambda: t0), 12 * 60, delta=1)
        past = t0 - timedelta(hours=1)
        self.assertEqual(engine._quota_wait_seconds(cfg, past, lambda: t0), 0.0)
        self.assertEqual(engine._quota_wait_seconds(cfg, None, lambda: t0),
                         cfg.quota_poll_minutes * 60)

    def test_countdown_non_tty_single_line_in_bounded_segments(self):
        stream = io.StringIO()  # isatty() False
        sleeps: list[float] = []
        engine._countdown(150, "Quota reset", sleeps.append, stream=stream)
        # One message, but never one long sleep: the total is unchanged and no
        # single segment exceeds the constant.
        self.assertEqual(sum(sleeps), 150)
        self.assertLessEqual(max(sleeps), engine._COUNTDOWN_SEGMENT)
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertNotIn("\r", stream.getvalue())

    def test_countdown_non_tty_stop_lands_within_one_segment(self):
        """A stop request reaches a multi-hour quota wait promptly.

        The stdin stop channel calls _thread.interrupt_main(); on POSIX that
        only makes the exception pending until bytecode next runs, which is
        the end of a segment. The injected sleep stands in for that delivery.
        """
        stream = io.StringIO()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            engine._countdown(10405, "Quota reset", sleep, segment=0.5,
                              stream=stream)
        self.assertEqual(sleeps, [0.5])

    def test_countdown_stops_counting_down_once_a_stop_is_requested(self):
        """A woken segment must not be followed by the rest of a multi-hour
        wait; the pending KeyboardInterrupt lands at the next bytecode."""
        self.addCleanup(clear_stop_wake)
        stream = io.StringIO()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            wake_stop_waiters()   # what the stdin watcher does mid-wait

        engine._countdown(10405, "Quota reset", sleep, stream=stream)
        self.assertEqual(sleeps, [engine._COUNTDOWN_SEGMENT])

    def test_stale_stop_request_does_not_shorten_a_later_countdown(self):
        """`run` is also a library and test entry point, so one stop request
        must not make every later countdown return immediately."""
        self.addCleanup(clear_stop_wake)
        wake_stop_waiters()
        stream = io.StringIO()
        sleeps: list[float] = []
        engine._countdown(150, "Quota reset", sleeps.append, stream=stream)
        self.assertEqual(sum(sleeps), 150)

    def test_countdown_tty_updates_in_place(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        stream = Tty()
        sleeps: list[float] = []
        engine._countdown(3, "Quota reset", sleeps.append, stream=stream)
        out = stream.getvalue()
        self.assertEqual(sum(sleeps), 3)
        self.assertGreaterEqual(out.count("\r"), 3)
        self.assertNotIn("\n", out)


if __name__ == "__main__":
    unittest.main()
