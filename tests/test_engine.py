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

from agents import engine
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
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls: list[tuple[str, str, str | None]] = []

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
        self._git("init")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        self.plan_dir = self.root / ".agents" / "plan01"
        self.plan_dir.mkdir(parents=True)

    def _git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              encoding="utf-8", check=True).stdout

    def build(self, retry=1, git_enabled=True):
        (self.root / ".agents" / "agents.toml").write_text(
            '[plan]\ntasks = "plan01"\n'
            f"[git]\nenabled = {'true' if git_enabled else 'false'}\n"
            f"[run]\nretry_per_task = {retry}\n"
            '[adapter]\nname = "claude"\n'
            '[adapter.claude]\ncommand = "python"\n',
            encoding="utf-8")
        return load_config(self.root / ".agents" / "agents.toml")

    def write_task(self, num, slug="task", **kw) -> Path:
        path = self.plan_dir / f"t{num:03d}_{slug}.toml"
        path.write_text(task_text(**kw), encoding="utf-8", newline="\n")
        return path

    def commit_all(self, message="init"):
        self._git("add", "-A")
        self._git("commit", "-m", message)

    def run_quiet(self, cfg, **kw) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return engine.run(cfg, **kw)

    def subjects(self) -> list[str]:
        return self._git("log", "--pretty=%s").splitlines()

    # AI 行為模擬
    def ai_done(self, task_path, files=None):
        def step(prompt):
            for rel, content in (files or {}).items():
                p = self.root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            set_status(task_path, "DONE")
            append_entry(journal_path_for(task_path), by="ai", event="done",
                         summary="完成")
            return ok_result()
        return step


class TestRunSuccess(EngineTestCase):
    def test_once_success_creates_checkpoint(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path, {"src/a.py": "x"})])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue(any(s.startswith("auto(t001)") for s in self.subjects()))
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
        adapter = ScriptedAdapter([self.ai_done(p1), self.ai_done(p2)])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)
        self.assertEqual(parse_task_file(p2).status, "DONE")
        autos = [s for s in self.subjects() if s.startswith("auto(")]
        self.assertEqual(len(autos), 2)

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
        self.assertIn(".agents/plan01/t001_task.toml", prompt)
        self.assertIn(".agents/plan01/r001_task.toml", prompt)


class TestAcceptanceGates(EngineTestCase):
    def test_self_blocked_committed_without_verify(self):
        # verify 是必失敗命令:自標 BLOCKED 免驗才會過(有實作才驗)
        path = self.write_task(1, verify=_FAILV)
        cfg = self.build()
        self.commit_all()

        def step(prompt):
            (self.root / "src").mkdir(exist_ok=True)
            (self.root / "src" / "half.py").write_text("x", encoding="utf-8")
            set_status(path, "BLOCKED")
            append_entry(journal_path_for(path), by="ai", event="blocked",
                         summary="卡在相依")
            return ok_result()

        self.assertEqual(self.run_quiet(cfg, once=True,
                                        adapter=ScriptedAdapter([step])), 0)
        self.assertTrue(any("BLOCKED" in s for s in self.subjects()))
        self.assertIn("src/half.py", self._git("ls-files"))

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
            (self.root / "outside.py").write_text("x", encoding="utf-8")
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([bad, lambda p: ok_result()])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertIn("原因", adapter.calls[1][0])          # 重試提示含失敗原因
        self.assertIn("outside.py", self._git("ls-files"))  # 產出不丟棄,收進檢查點
        from agents.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(e["by"] == "scheduler" and e["event"] == "blocked"
                            for e in entries))

    def test_verify_failure_then_success_on_retry(self):
        path = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build(retry=1)
        self.commit_all()

        def first(prompt):
            set_status(path, "DONE")
            return ok_result()

        def second(prompt):
            (self.root / "src").mkdir(exist_ok=True)
            (self.root / "src" / "ok.txt").write_text("y", encoding="utf-8")
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
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([step])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")


class TestQuotaAndResume(EngineTestCase):
    def test_quota_waits_then_resumes_same_task(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        reset = t0 + timedelta(minutes=5)
        sleeps: list[float] = []

        def quota_step(prompt):
            (self.root / "src").mkdir(exist_ok=True)
            (self.root / "src" / "partial.py").write_text("p", encoding="utf-8")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=reset)

        adapter = ScriptedAdapter([quota_step, self.ai_done(path)])
        rc = self.run_quiet(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), (5 + 2) * 60, delta=1)  # +2 分鐘緩衝
        self.assertIn("接續", adapter.calls[1][0])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(t001)") for s in subjects))
        self.assertTrue(any(s.startswith("auto(t001)") for s in subjects))
        from agents.plan import read_entries
        self.assertTrue(any(e["event"] == "quota"
                            for e in read_entries(journal_path_for(path))))

    def test_wip_task_resumed_on_startup(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("接續", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestSchedulingAndRefusals(EngineTestCase):
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
        (self.root / "dirty.txt").write_text("x", encoding="utf-8")
        cfg = self.build()
        self.assertEqual(self.run_quiet(cfg, adapter=ScriptedAdapter([])), 1)

    def test_bad_task_file_refused_before_any_session(self):
        (self.plan_dir / "t001_bad.toml").write_text("status = [", encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 1)
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

    def test_report_lists_checkpoints_and_blocked_summary(self):
        p1 = self.write_task(1)
        p2 = self.write_task(2, verify=_FAILV, title="會卡住")
        cfg = self.build(retry=0)
        self.commit_all()

        def fail_step(prompt):
            set_status(p2, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([self.ai_done(p1), fail_step])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        from agents.plan import Plan
        text = engine.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("t001  DONE", text)
        self.assertIn("t002  BLOCKED", text)
        self.assertIn("最後日誌(scheduler)", text)
        self.assertIn("[", text)  # DONE 任務附檢查點 hash
        # _report.md 已寫出,但不進版控
        self.assertTrue((cfg.tasks_dir / "_report.md").is_file())
        self.assertNotIn("_report.md", self._git("ls-files"))


if __name__ == "__main__":
    unittest.main()
