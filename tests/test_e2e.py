"""端到端演練:臨時 repo + 工作資料夾 + 可編劇本的 fake adapter,四劇本整合測試。

獨立成一份測試基建(不跨檔匯入其他 test_*.py),與其餘測試檔慣例一致。
"""
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
from agents.plan import (append_entry, journal_path_for, parse_task_file,
                         set_status)

_OK = 'python -c "raise SystemExit(0)"'


def task_text(*, title="任務", deps=(), status="TODO",
              scope=("src/",), verify=_OK) -> str:
    return "\n".join([
        f"title = {json.dumps(title, ensure_ascii=False)}",
        "deps = [" + ", ".join(json.dumps(d) for d in deps) + "]",
        'model = "lite"',
        f"status = {json.dumps(status)}",
        "scope = [" + ", ".join(json.dumps(s) for s in scope) + "]",
        f"verify = {json.dumps(verify, ensure_ascii=False)}",
        'goal = """\n做事。\n"""',
        'acceptance = """\n- ok\n"""',
    ]) + "\n"


def ok_result() -> TaskResult:
    return TaskResult(exit_code=0, output="", quota_exhausted=False, reset_at=None)


class ScriptedAdapter(Adapter):
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def run_task(self, prompt, model, effort, cwd):
        self.calls.append(prompt)
        step = self.steps.pop(0)
        return step(prompt) if callable(step) else step


class E2ETestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._git("init")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        self.plan_dir = self.root / ".agents" / "plan01"
        self.plan_dir.mkdir(parents=True)
        (self.root / ".agents" / "agents.toml").write_text(
            '[plan]\ntasks = "plan01"\n[run]\nretry_per_task = 1\n',
            encoding="utf-8")

    def _git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              encoding="utf-8", check=True).stdout

    def cfg(self):
        return load_config(self.root / ".agents" / "agents.toml")

    def add_task(self, num, **kw) -> Path:
        path = self.plan_dir / f"t{num:03d}_task.toml"
        path.write_text(task_text(**kw), encoding="utf-8", newline="\n")
        return path

    def start(self):
        self._git("add", "-A")
        self._git("commit", "-m", "init")

    def done_step(self, path, files=None):
        def step(prompt):
            for rel, content in (files or {}).items():
                p = self.root / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="ai", event="done",
                         summary="完成")
            return ok_result()
        return step

    def run_engine(self, adapter, **kw) -> int:
        with contextlib.redirect_stdout(io.StringIO()):
            return engine.run(self.cfg(), adapter=adapter, **kw)

    def subjects(self):
        return self._git("log", "--pretty=%s").splitlines()


class TestScenarios(E2ETestCase):
    def test_smooth_run_three_tasks(self):
        """順利劇本:三任務依序 DONE,三個 checkpoint,樹乾淨。"""
        p1 = self.add_task(1)
        p2 = self.add_task(2, deps=("t001",))
        p3 = self.add_task(3, deps=("t002",))
        self.start()
        adapter = ScriptedAdapter([
            self.done_step(p1, {"src/a.py": "a"}),
            self.done_step(p2, {"src/b.py": "b"}),
            self.done_step(p3, {"src/c.py": "c"}),
        ])
        self.assertEqual(self.run_engine(adapter), 0)
        autos = [s for s in self.subjects() if s.startswith("auto(")]
        self.assertEqual(len(autos), 3)
        for p in (p1, p2, p3):
            self.assertEqual(parse_task_file(p).status, "DONE")
        porcelain = [ln for ln in self._git("status", "--porcelain").splitlines()
                     if ln.strip() and "report.md" not in ln
                     and "agents.lock" not in ln]
        self.assertEqual(porcelain, [])

    def test_fail_retry_then_pass(self):
        """失敗重試劇本:首輪留越界檔 -> 重試提示含原因 -> 次輪補救通過。
        產出絕不丟棄:越界檔最終仍在檢查點裡(次輪把它移進 scope)。"""
        p1 = self.add_task(1, scope=("src/", "outside.py"))
        self.start()

        def first(prompt):
            (self.root / "outside_tmp.py").write_text("x", encoding="utf-8")
            set_status(p1, "DONE")
            return ok_result()

        def second(prompt):
            # 修正:把越界檔移到 scope 內名稱
            (self.root / "outside_tmp.py").rename(self.root / "outside.py")
            return ok_result()

        adapter = ScriptedAdapter([first, second])
        self.assertEqual(self.run_engine(adapter, once=True), 0)
        self.assertIn("原因", adapter.calls[1])
        self.assertEqual(parse_task_file(p1).status, "DONE")
        self.assertIn("outside.py", self._git("ls-files"))

    def test_blocked_gates_downstream_others_proceed(self):
        """BLOCKED 劇本:t001 重試用盡 -> 調度器標 BLOCKED + r 檔記錄;
        依賴它的 t002 被擋,無依賴的 t003 照跑,全在同一次 run 內。"""
        p1 = self.add_task(1)
        p2 = self.add_task(2, deps=("t001",))
        p3 = self.add_task(3)
        self.start()

        def bad(prompt):
            (self.root / "rogue.py").write_text("x", encoding="utf-8")
            set_status(p1, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([bad, lambda p: ok_result(),
                                   self.done_step(p3)])
        self.assertEqual(self.run_engine(adapter), 0)
        self.assertEqual(parse_task_file(p1).status, "BLOCKED")
        self.assertEqual(parse_task_file(p2).status, "TODO")
        self.assertEqual(parse_task_file(p3).status, "DONE")
        from agents.plan import read_entries
        self.assertTrue(any(e["event"] == "blocked"
                            for e in read_entries(journal_path_for(p1))))

    def test_quota_interrupt_wip_then_resume(self):
        """額度劇本:第一輪額度耗盡 -> wip 檢查點 + r 檔 quota 記錄 -> 假時鐘
        等待 5+2 分鐘 -> 帶接續提示重跑同一任務成功。"""
        p1 = self.add_task(1)
        self.start()
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        sleeps: list[float] = []

        def quota_step(prompt):
            (self.root / "src").mkdir(exist_ok=True)
            (self.root / "src" / "half.py").write_text("h", encoding="utf-8")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=t0 + timedelta(minutes=5))

        adapter = ScriptedAdapter([quota_step, self.done_step(p1)])
        rc = self.run_engine(adapter, sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), 420, delta=1)
        self.assertIn("接續", adapter.calls[1])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(t001)") for s in subjects))
        self.assertTrue(any(s.startswith("auto(t001)") for s in subjects))
        self.assertEqual(parse_task_file(p1).status, "DONE")


if __name__ == "__main__":
    unittest.main()
