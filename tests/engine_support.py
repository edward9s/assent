"""Shared fixtures for the engine, inspection and preflight test modules.

One temporary git repository, one scripted adapter, and the small helpers that
build a task folder in it.  The real AI CLI is never touched: every session is a
``ScriptedAdapter`` step, and sleep/now are injected by the tests that need them.

Chinese literals that remain here and in the modules using these fixtures are
deliberate user/upstream passthrough data (task titles, notes, journal
summaries, AGENTS.md content) used to prove that non-English data flows through
verbatim.
"""
import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import engine, gitops
from assent.adapters import Adapter, TaskResult
from assent.config import load_config
from assent.plan import append_entry, journal_path_for, set_status
from tests.link_support import safe_rmtree

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
            raise AssertionError("adapter called more times than the script allows")
        step = self.steps.pop(0)
        return step(prompt) if callable(step) else step


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(safe_rmtree, self.root)
        self.worktrees_root = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(safe_rmtree, self.worktrees_root)
        self._git("init")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "AGENTS.md").write_text("專案規則\n", encoding="utf-8")
        self.plan_dir = self.root / ".assent" / "plan01"
        self.plan_dir.mkdir(parents=True)
        # These tests exercise task-session scheduling and focused checkpoint
        # gates.  Full candidate verification has its own engine handoff tests.
        verifier = mock.patch(
            "assent.engine.verification.verify_folder_if_needed", return_value=0)
        verifier.start()
        self.addCleanup(verifier.stop)

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

    def build(self, retry=1, adapter_name="claude", extra_config=""):
        (self.root / ".assent" / "assent.toml").write_text(
            f"[run]\nretry_per_task = {retry}\n"
            f'[adapter]\nname = "{adapter_name}"\n'
            '[adapter.claude]\ncommand = "python"\n'
            + extra_config,
            encoding="utf-8")
        return load_config(self.root / ".assent" / "assent.toml", "plan01")

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

    # AI behavior simulation
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
