"""engine 測試:注入 ScriptedAdapter + 假 sleep/now,在臨時 repo 驗證
選任務 -> 驗收 -> 寫回的全部分支。真實 CLI 一律不碰。"""
import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from agents import AgentsError, engine, gitops
from agents.adapters import Adapter, TaskResult
from agents.config import load_config
from agents.plan import append_entry, journal_path_for, parse_task_file, set_status

_OK = 'python -c "raise SystemExit(0)"'
_FAILV = 'python -c "raise SystemExit(3)"'
_NEEDS_OK_TXT = ('python -c "import pathlib,sys;'
                 "sys.exit(0 if pathlib.Path('src/ok.txt').exists() else 1)\"")


def task_text(*, title="任務", deps=(), model="lite", effort=None,
              status="TODO", scope=("src/",), verify=_OK,
              goal="做一件事。", acceptance="- 完成", notes="") -> str:
    lines = [
        f"title = {json.dumps(title, ensure_ascii=False)}",
        "deps = [" + ", ".join(json.dumps(d) for d in deps) + "]",
        f"model = {json.dumps(model)}",
    ]
    if effort:
        lines.append(f"effort = {json.dumps(effort)}")
    lines += [
        f"status = {json.dumps(status)}",
        "scope = [" + ", ".join(json.dumps(s) for s in scope) + "]",
        f"verify = {json.dumps(verify, ensure_ascii=False)}",
        f'goal = """\n{goal}\n"""',
        f'acceptance = """\n{acceptance}\n"""',
    ]
    if notes:
        lines.append(f'notes = """\n{notes}\n"""')
    return "\n".join(lines) + "\n"


def ok_result() -> TaskResult:
    return TaskResult(exit_code=0, output="", quota_exhausted=False, reset_at=None)


class ScriptedAdapter(Adapter):
    def __init__(self, steps, resolved_model=None):
        self.steps = list(steps)
        self.calls: list[tuple[str, str, str | None]] = []
        self.resolved_model = resolved_model
        self.resolve_calls: list[str] = []

    def resolve_model(self, model):
        self.resolve_calls.append(model)
        return self.resolved_model or model

    def run_task(self, prompt, model, effort, cwd):
        self.calls.append((prompt, model, effort))
        if not self.steps:
            raise AssertionError("adapter 被呼叫的次數超出劇本")
        step = self.steps.pop(0)
        return step(prompt) if callable(step) else step


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.worktrees_root = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(shutil.rmtree, self.worktrees_root, ignore_errors=True)
        self._git("init")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".agents/\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("專案規則\n", encoding="utf-8")
        self.plan_dir = self.root / ".agents" / "plan01"
        self.plan_dir.mkdir(parents=True)

    def _git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              encoding="utf-8", check=True).stdout

    def execution_root(self) -> Path:
        candidate = gitops.worktree_path(self.root, "plan01")
        return candidate if candidate.exists() else self.root

    def _git_execution(self, *args) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.execution_root(), capture_output=True,
            encoding="utf-8", check=True).stdout

    def build(self, retry=1, adapter_name="claude", prompt_template=None):
        prompt = (f'[prompt]\ntemplate = {json.dumps(prompt_template)}\n'
                  if prompt_template is not None else "")
        (self.root / ".agents" / "agents.toml").write_text(
            f"[run]\nretry_per_task = {retry}\n"
            f'[adapter]\nname = "{adapter_name}"\n'
            '[adapter.claude]\ncommand = "python"\n'
            + prompt,
            encoding="utf-8")
        return load_config(self.root / ".agents" / "agents.toml", "plan01")

    def write_task(self, num, slug="task", **kw) -> Path:
        path = self.plan_dir / f"t{num:03d}_{slug}.e.toml"
        path.write_text(task_text(**kw), encoding="utf-8", newline="\n")
        return path

    def commit_all(self, message="init"):
        self._git("add", "-A")
        self._git("commit", "-m", message)

    def run_quiet(self, cfg, **kw) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return engine.run(cfg, **kw)

    def subjects(self) -> list[str]:
        return self._git_execution("log", "--pretty=%s").splitlines()

    # AI 行為模擬
    def ai_done(self, task_path, files=None, *, by="claude",
                requested_model="lite"):
        def step(prompt):
            for rel, content in (files or {}).items():
                p = self.execution_root() / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            set_status(task_path, "DONE")
            append_entry(journal_path_for(task_path), by=by,
                         requested_model=requested_model, event="done",
                         summary="完成")
            return ok_result()
        return step


class TestRunSuccess(EngineTestCase):
    def test_unfinished_folder_prerequisite_refuses_before_lock(self):
        self.write_task(1)
        base = self.root / ".agents" / "base"
        base.mkdir()
        for index, status in enumerate(("TODO", "WIP", "BLOCKED", "DONE"), 1):
            (base / f"t{index:03d}_task.e.toml").write_text(
                task_text(status=status), encoding="utf-8")
        (self.plan_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([])

        for options in ({"once": True}, {"task_id": "t001"}):
            with self.subTest(options=options), mock.patch(
                    "agents.engine.lockfile.hold_lock") as hold_lock:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = engine.run(cfg, adapter=adapter, **options)
                self.assertEqual(result, 1)
                self.assertIn(
                    "前置資料夾 base 尚有 3 個未完成任務"
                    "(TODO 1、WIP 1、BLOCKED 1)", out.getvalue())
                hold_lock.assert_not_called()
        self.assertEqual(adapter.calls, [])

    def test_done_and_skip_folder_prerequisite_allows_run(self):
        path = self.write_task(1)
        base = self.root / ".agents" / "base"
        base.mkdir()
        for index, status in enumerate(("DONE", "SKIP"), 1):
            (base / f"t{index:03d}_task.e.toml").write_text(
                task_text(status=status), encoding="utf-8")
        (self.plan_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path, {"src/result.py": "ok"})])

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(adapter.calls), 1)

    def test_task_runs_with_prompt_scope_and_journal_paths(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([
            self.ai_done(path, {"src/formal.py": "ok"})])

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        prompt = adapter.calls[0][0]
        journal = path.with_name("t001_task.r.toml")
        self.assertIn(str(path), prompt)
        self.assertIn(str(journal), prompt)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue(journal.is_file())
        self.assertFalse(path.with_name("t001_task.e.r.toml").exists())
        self.assertTrue(any(s.startswith("auto(plan01/t001): ")
                            for s in self.subjects()))

    def test_once_success_creates_checkpoint(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path, {"src/a.py": "x"})])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue(path.with_name("t001_task.r.toml").is_file())
        self.assertFalse(path.with_name("r001_task.toml").exists())
        subject = next(s for s in self.subjects()
                       if s.startswith("auto(plan01/t001): "))
        self.assertNotIn("Co-Authored-By", subject)
        self.assertNotIn("Generated with", subject)
        # 工作樹除 _report.md、agents.lock(執行期產物)外乾淨
        porcelain = [ln for ln in self._git("status", "--porcelain").splitlines()
                     if ln.strip() and "_report.md" not in ln
                     and "agents.lock" not in ln]
        self.assertEqual(porcelain, [])

    def test_two_tasks_run_to_completion(self):
        p1 = self.write_task(1)
        p2 = self.write_task(2, deps=("t001",))
        cfg = self.build()
        self.commit_all()

        def second_task(prompt):
            report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
            self.assertIn("t001  DONE", report)
            self.assertIn("t002  TODO", report)
            return self.ai_done(p2, {"src/two.py": "2"})(prompt)

        adapter = ScriptedAdapter([
            self.ai_done(p1, {"src/one.py": "1"}), second_task])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)
        self.assertEqual(parse_task_file(p2).status, "DONE")
        autos = [s for s in self.subjects() if s.startswith("auto(")]
        self.assertEqual(len(autos), 2)

    def test_report_write_failure_does_not_change_successful_task_result(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path, {"src/a.py": "x"})])

        with mock.patch.object(
                engine, "write_report", side_effect=PermissionError("報告檔被鎖定")):
            self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue(any(s.startswith("auto(plan01/t001): ")
                            for s in self.subjects()))

    def test_try_write_report_does_not_swallow_process_control_exceptions(self):
        cfg = self.build()
        self.write_task(1)
        with mock.patch.object(engine, "write_report", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                engine._try_write_report(cfg)

    def test_report_updates_after_scheduler_blocked_before_next_task(self):
        p1 = self.write_task(1)
        p2 = self.write_task(2)
        cfg = self.build(retry=0)
        self.commit_all()

        def second_task(prompt):
            report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
            self.assertIn("t001  BLOCKED", report)
            return self.ai_done(p2)(prompt)

        adapter = ScriptedAdapter([lambda prompt: ok_result(), second_task])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)
        self.assertEqual(parse_task_file(p1).status, "BLOCKED")
        self.assertEqual(parse_task_file(p2).status, "DONE")

    def test_effort_from_task_overrides_default(self):
        p1 = self.write_task(1, effort="low")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][2], "low")

    def test_effort_default_applied_per_tier(self):
        p1 = self.write_task(1, model="lite")  # 內建 lite 預設 medium
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0], (adapter.calls[0][0], "lite", "medium"))

    def test_prompt_contains_task_and_journal_paths(self):
        p1 = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        prompt = adapter.calls[0][0]
        self.assertIn(str(cfg.agents_dir / "instructions.md"), prompt)
        self.assertIn(str(p1), prompt)
        self.assertIn(str(p1.with_name("t001_task.r.toml")), prompt)
        self.assertIn(_OK, prompt)
        self.assertIn('by = "claude"', prompt)
        self.assertIn('requested_model = "lite"', prompt)

    def test_codex_prompt_uses_resolved_cli_model(self):
        p1 = self.write_task(1)
        cfg = self.build(adapter_name="codex")
        self.commit_all()

        def done(prompt):
            set_status(p1, "DONE")
            append_entry(journal_path_for(p1), by="codex",
                         requested_model="gpt-cli", event="done",
                         summary="完成")
            return ok_result()

        adapter = ScriptedAdapter([done], resolved_model="gpt-cli")
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        prompt, requested_model, _ = adapter.calls[0]
        self.assertIn('by = "codex"', prompt)
        self.assertIn('requested_model = "gpt-cli"', prompt)
        self.assertEqual(requested_model, "gpt-cli")

    def test_custom_prompt_can_use_agent_and_requested_model(self):
        p1 = self.write_task(1)
        cfg = self.build(
            prompt_template="{agent}|{requested_model}|{task_id}")
        self.commit_all()
        adapter = ScriptedAdapter(
            [self.ai_done(p1, requested_model="cli-model")],
            resolved_model="cli-model")
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][0], "claude|cli-model|t001")

    def test_worktree_default_verify_uses_main_script_and_worktree_cwd(self):
        cfg = self.build()
        worktree = self.root / "isolated"
        worktree.mkdir()
        (cfg.agents_dir / "verify.py").write_text(
            "from pathlib import Path\n"
            "Path('verified.txt').write_text('ok', encoding='utf-8')\n",
            encoding="utf-8")

        self.assertEqual(engine._run_verify(
            cfg.for_worktree(worktree), "python .agents/verify.py"), 0)
        self.assertEqual((worktree / "verified.txt").read_text(encoding="utf-8"),
                         "ok")
        self.assertFalse((self.root / "verified.txt").exists())


class TestAcceptanceGates(EngineTestCase):
    def test_self_blocked_committed_without_verify(self):
        # verify 是必失敗命令:自標 BLOCKED 免驗才會過(有實作才驗)
        path = self.write_task(1, verify=_FAILV)
        cfg = self.build()
        self.commit_all()

        def step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "half.py").write_text("x", encoding="utf-8")
            set_status(path, "BLOCKED")
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="blocked",
                         summary="卡在相依")
            return ok_result()

        self.assertEqual(self.run_quiet(cfg, once=True,
                                        adapter=ScriptedAdapter([step])), 0)
        self.assertTrue(any("BLOCKED" in s for s in self.subjects()))
        self.assertIn("src/half.py", self._git_execution("ls-files"))

    def test_status_not_updated_fails(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()
        adapter = ScriptedAdapter([lambda p: ok_result()])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")  # 調度器標

    def test_scope_violation_retry_then_blocked_keeps_output(self):
        path = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        def bad(prompt):
            (self.execution_root() / "outside.py").write_text(
                "x", encoding="utf-8")
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([bad, lambda p: ok_result()])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertIn("原因", adapter.calls[1][0])          # 重試提示含失敗原因
        self.assertIn("outside.py", self._git_execution(
            "ls-files"))  # 產出不丟棄,收進檢查點
        from agents.plan import read_entries
        entries = read_entries(journal_path_for(path))
        blocked = next(e for e in entries if e["by"] == "scheduler"
                       and e["event"] == "blocked")
        self.assertEqual(blocked["agent"], "claude")
        self.assertEqual(blocked["requested_model"], "lite")

    def test_verify_failure_then_success_on_retry(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(retry=1)
        self.commit_all()

        def first(prompt):
            set_status(path, "DONE")
            return ok_result()

        def second(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "ok.txt").write_text("y", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([first, second])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_tampered_task_file_detected(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def tamper(prompt):
            # 執行 AI 放寬自己的 scope + verify,並自標 DONE
            path.write_text(task_text(status="DONE", scope=("src/", "secret/"),
                                      verify="echo ok"),
                            encoding="utf-8", newline="\n")
            return ok_result()

        self.run_quiet(cfg, once=True, adapter=ScriptedAdapter([tamper]))
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        from agents.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any("欄位" in e["summary"] for e in entries
                            if e["by"] == "scheduler"))

    def test_retry_zero_blocks_after_single_attempt(self):
        path = self.write_task(1, verify=_FAILV)
        cfg = self.build(retry=0)
        self.commit_all()

        def step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "failed.py").write_text(
                "x", encoding="utf-8")
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([step])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertTrue(any(
            s.startswith("auto(plan01/t001): BLOCKED - ")
            for s in self.subjects()))

    def test_self_blocked_creates_namespaced_checkpoint(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text(
                "x", encoding="utf-8")
            set_status(path, "BLOCKED")
            return ok_result()

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([step])), 0)
        self.assertTrue(any(
            s == "auto(plan01/t001): BLOCKED(執行 AI 自標)"
            for s in self.subjects()))


class TestQuotaAndResume(EngineTestCase):
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
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=reset)

        adapter = ScriptedAdapter([quota_step, self.ai_done(path)])
        rc = self.run_quiet(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), (5 + 2) * 60, delta=1)  # +2 分鐘緩衝
        self.assertIn("接續", adapter.calls[1][0])
        self.assertEqual(adapter.resolve_calls, ["lite", "lite"])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in subjects))
        from agents.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "lite")
        self.assertNotIn("session", [e["event"] for e in entries])

    def test_wip_task_resumed_on_startup(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("接續", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestInterruptedTaskResume(EngineTestCase):
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
        from agents.plan import read_entries
        entries = read_entries(journal_path_for(path))
        interrupt = next(e for e in entries
                         if e["by"] == "scheduler"
                         and e["event"] == "interrupt"
                         and "使用者中斷" in e["summary"])
        self.assertEqual(interrupt["agent"], "claude")
        self.assertEqual(interrupt["requested_model"], "lite")

        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("接續", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_agents_error_marks_current_task_wip_and_keeps_exit_code(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        def failed(prompt):
            raise AgentsError("連線中斷")

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([failed])), 1)
        self.assertEqual(parse_task_file(path).status, "WIP")
        from agents.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(e["event"] == "interrupt"
                            and "基礎設施錯誤" in e["summary"]
                            for e in entries))

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
        from agents.plan import read_entries
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


class TestSchedulingAndRefusals(EngineTestCase):
    def test_run_and_check_refuse_root_without_own_git_marker(self):
        nested_root = self.root / "not-repo"
        nested_plan = nested_root / ".agents" / "plan01"
        nested_plan.mkdir(parents=True)
        config = nested_root / ".agents" / "agents.toml"
        config.write_text("", encoding="utf-8")
        cfg = load_config(config, "plan01")
        adapter = ScriptedAdapter([])

        for name, operation in (
                ("run", lambda: engine.run(cfg, adapter=adapter)),
                ("check", lambda: engine.check(cfg))):
            with self.subTest(command=name):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(operation(), 1)
                self.assertIn(
                    "本專案尚未初始化 git,請先執行 git init", out.getvalue())

        self.assertEqual(adapter.calls, [])
        self.assertFalse((nested_plan / "agents.lock").exists())

    def test_blocked_gates_downstream_but_others_run(self):
        p1 = self.write_task(1, verify=_FAILV)
        p2 = self.write_task(2, deps=("t001",))
        p3 = self.write_task(3)
        cfg = self.build(retry=0)
        self.commit_all()

        def fail_step(prompt):
            set_status(p1, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([fail_step, self.ai_done(p3)])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)
        self.assertEqual(parse_task_file(p1).status, "BLOCKED")
        self.assertEqual(parse_task_file(p2).status, "TODO")   # 被前置擋住
        self.assertEqual(parse_task_file(p3).status, "DONE")   # 無依賴照跑

    def test_task_flag_rejects_unmet_deps(self):
        self.write_task(1)
        self.write_task(2, deps=("t001",))
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([])
        self.assertEqual(self.run_quiet(cfg, task_id="t002", adapter=adapter), 1)
        self.assertEqual(adapter.calls, [])

    def test_task_flag_skips_settled_task(self):
        self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        self.assertEqual(self.run_quiet(cfg, task_id="t001",
                                        adapter=ScriptedAdapter([])), 0)

    def test_dirty_tree_refused(self):
        self.write_task(1)
        self.commit_all()
        worktree = gitops.ensure_worktree(self.root, "plan01")
        (worktree / "dirty.txt").write_text("x", encoding="utf-8")
        cfg = self.build()
        self.assertEqual(self.run_quiet(cfg, adapter=ScriptedAdapter([])), 1)

    def test_bad_task_file_refused_before_any_session(self):
        (self.plan_dir / "t001_bad.e.toml").write_text(
            "status = [", encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 1)
        self.assertEqual(adapter.calls, [])

    def test_retired_task_file_makes_check_and_run_fail_closed(self):
        (self.plan_dir / "t001_old.toml").write_text(
            task_text(), encoding="utf-8", newline="\n")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([])

        check_out = io.StringIO()
        with contextlib.redirect_stdout(check_out):
            self.assertEqual(engine.check(cfg), 1)
        self.assertIn("舊任務檔", check_out.getvalue())
        self.assertIn("搬移", check_out.getvalue())

        run_out = io.StringIO()
        with contextlib.redirect_stdout(run_out):
            self.assertEqual(engine.run(cfg, adapter=adapter), 1)
        self.assertIn("舊任務檔", run_out.getvalue())
        self.assertIn("搬移", run_out.getvalue())
        self.assertEqual(adapter.calls, [])


class TestQuotaMath(EngineTestCase):
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

    def test_countdown_non_tty_single_line(self):
        stream = io.StringIO()  # isatty() False
        sleeps: list[float] = []
        engine._countdown(90, "額度重置", sleeps.append, stream=stream)
        self.assertEqual(sleeps, [90])
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertNotIn("\r", stream.getvalue())

    def test_countdown_tty_updates_in_place(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True

        stream = Tty()
        sleeps: list[float] = []
        engine._countdown(3, "額度重置", sleeps.append, stream=stream)
        out = stream.getvalue()
        self.assertEqual(sum(sleeps), 3)
        self.assertGreaterEqual(out.count("\r"), 3)
        self.assertNotIn("\n", out)


class TestQueries(EngineTestCase):
    def test_status_reports_counts_and_next(self):
        self.write_task(1, status="DONE")
        self.write_task(2, deps=("t001",), title="第二個")
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.status(cfg), 0)
        text = out.getvalue()
        self.assertIn("DONE 1", text)
        self.assertIn("t002", text)
        self.assertIn("第二個", text)

    def test_check_passes_on_valid_setup(self):
        self.write_task(1)
        cfg = self.build()
        self.commit_all()  # claude command = python,--version 必然可執行
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 0)
        self.assertIn("結果:通過", out.getvalue())

    def test_check_fails_on_dependency_cycle(self):
        self.write_task(1, deps=("t002",))
        self.write_task(2, deps=("t001",))
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 1)
        self.assertIn("FAIL", out.getvalue())

    def test_check_validates_selected_folder_declaration(self):
        self.write_task(1)
        (self.plan_dir / "_folder.toml").write_text(
            'after = []\nunknown = true\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 1)
        self.assertIn("資料夾依賴:FAIL", out.getvalue())
        self.assertIn("未知鍵", out.getvalue())

    def test_report_lists_checkpoints_and_blocked_summary(self):
        p1 = self.write_task(1)
        p2 = self.write_task(2, verify=_FAILV, title="會卡住")
        cfg = self.build(retry=0)
        self.commit_all()

        def fail_step(prompt):
            set_status(p2, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([
            self.ai_done(p1, {"src/done.py": "ok"}), fail_step])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        from agents.plan import Plan
        text = engine.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("t001  DONE", text)
        self.assertIn("t002  BLOCKED", text)
        self.assertIn("最後日誌(scheduler)", text)
        self.assertIn("[", text)  # DONE 任務附檢查點 hash
        # _report.md 已寫出,但不進版控
        self.assertTrue((cfg.tasks_dir / "_report.md").is_file())
        self.assertNotIn("_report.md", self._git_execution("ls-files"))

    def test_report_isolates_namespaced_checkpoints(self):
        self.write_task(1, status="DONE", title="目前一")
        self.write_task(3, status="DONE", title="目前三")
        other_dir = self.root / ".agents" / "plan010"
        other_dir.mkdir()
        (other_dir / "t001_other.e.toml").write_text(
            task_text(status="DONE", title="其他一"), encoding="utf-8",
            newline="\n")
        (other_dir / "t003_other.e.toml").write_text(
            task_text(status="DONE", title="其他三"), encoding="utf-8",
            newline="\n")
        cfg = self.build()
        other_cfg = load_config(
            self.root / ".agents" / "agents.toml", folder="plan010")
        self.commit_all()

        def checkpoint(subject):
            self._git("commit", "--allow-empty", "-m", subject)
            return self._git("rev-parse", "--short", "HEAD").strip()

        other_t1 = checkpoint("auto(plan010/t001): 其他一")
        other_t3 = checkpoint("auto(plan010/t003): 其他三")
        legacy_t3 = checkpoint("auto(t003): 舊格式歸屬不明")
        wrong_id = checkpoint("auto(plan01/t0010): 任務 id 僅為前綴")
        current_t1 = checkpoint("auto(plan01/t001): 目前一")
        current_t3 = checkpoint("auto(plan01/t003): 目前三")

        current = engine.render_report(cfg, engine.Plan.parse(cfg.tasks_dir))
        other = engine.render_report(
            other_cfg, engine.Plan.parse(other_cfg.tasks_dir))

        self.assertIn(f"t001  DONE     目前一  [{current_t1}]", current)
        self.assertIn(f"t003  DONE     目前三  [{current_t3}]", current)
        self.assertNotIn(other_t1, current)
        self.assertNotIn(other_t3, current)
        self.assertNotIn(legacy_t3, current)
        self.assertNotIn(wrong_id, current)
        self.assertIn(f"t001  DONE     其他一  [{other_t1}]", other)
        self.assertIn(f"t003  DONE     其他三  [{other_t3}]", other)
        self.assertNotIn(current_t1, other)
        self.assertNotIn(current_t3, other)
        self.assertIn("進度:DONE 2 / BLOCKED 0 / WIP 0 / TODO 0 / SKIP 0(共 2)",
                      current)

    def test_report_reads_legacy_ai_entry_without_identity_fields(self):
        path = self.write_task(1, status="BLOCKED")
        journal_path_for(path).write_text(
            '[[entry]]\ntime = "2026-07-17T00:00:00+00:00"\n'
            'by = "ai"\nevent = "blocked"\nsummary = "舊日誌仍可讀"\n',
            encoding="utf-8")
        cfg = self.build()
        from agents.plan import Plan
        text = engine.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("最後日誌(ai):舊日誌仍可讀", text)


if __name__ == "__main__":
    unittest.main()
