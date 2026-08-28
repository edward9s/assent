"""Shared fixtures for the engine, inspection and preflight test modules.

One temporary git repository, one scripted adapter, and the small helpers that
build a plan in it.  The real AI CLI is never touched: every session is a
``ScriptedAdapter`` step, and sleep/now are injected by the tests that need them.

Chinese literals that remain here and in the modules using these fixtures are
deliberate user/upstream passthrough data (task titles, notes, journal
summaries, AGENTS.md content) used to prove that non-English data flows through
verbatim.
"""
import contextlib
import io
import json
import re
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


def task_text(*, title="任務", deps=(), model="lite",
              status="TODO", verify=_OK,
              goal="做一件事。", acceptance="- 完成", notes="") -> str:
    lines = [
        f"title = {json.dumps(title, ensure_ascii=False)}",
        "deps = [" + ", ".join(json.dumps(d) for d in deps) + "]",
        f"model = {json.dumps(model)}",
    ]
    lines += [
        f"status = {json.dumps(status)}",
        f"verify = {json.dumps(verify, ensure_ascii=False)}",
        f'goal = """\n{goal}\n"""',
        f'acceptance = """\n{acceptance}\n"""',
    ]
    if notes:
        lines.append(f'notes = """\n{notes}\n"""')
    return "\n".join(lines) + "\n"


# Assent ships no built-in model ids, so every fixture states its own tier table.
# These values are the fixtures' own, not a packaged default: assertions elsewhere in
# the suite name them directly.
TEST_MODELS = {
    "claude": 'prime = "fable/high"\ncore = "opus/high"\nlite = "sonnet/medium"\n',
    "codex": ('prime = "gpt-5.6-sol/high"\ncore = "gpt-5.6-terra/medium"\n'
              'lite = "gpt-5.6-luna/low"\n'),
    "antigravity": ('prime = "gemini-3.1-pro/high"\ncore = "gemini-3.6-flash/high"\n'
                    'lite = "gemini-3.5-flash/medium"\n'),
}


# The same tiers as TEST_MODELS, for fixtures that build a Config directly.
TEST_MODEL_TIERS = {
    "claude": {"prime": "fable/high", "core": "opus/high",
               "lite": "sonnet/medium"},
    "codex": {"prime": "gpt-5.6-sol/high", "core": "gpt-5.6-terra/medium",
              "lite": "gpt-5.6-luna/low"},
    "antigravity": {"prime": "gemini-3.1-pro/high",
                    "core": "gemini-3.6-flash/high",
                    "lite": "gemini-3.5-flash/medium"},
}


def models_block(text: str = "") -> str:
    """Tier tables for every adapter ``text`` mentions but states no models for.

    Assent ships no built-in model ids, so any adapter a config selects -- through the
    rotation or through a workflow step's own binding -- has to name its tiers. Supplying
    them for an adapter the document never reaches is harmless; skipping any table the
    document does state is what keeps a case about a partial or blank entry provable.
    """
    named = ["claude"] + [vendor for vendor in TEST_MODELS
                          if f'"{vendor}"' in text]
    return "".join(
        f"[adapter.{vendor}.models]\n{TEST_MODELS[vendor]}"
        for vendor in dict.fromkeys(named)
        if f"[adapter.{vendor}.models]" not in text)


def ok_result() -> TaskResult:
    return TaskResult(exit_code=0, output="", quota_exhausted=False, reset_at=None)


class ScriptedAdapter(Adapter):
    """A test double for one vendor CLI.

    ``resolved_model`` substitutes the model half of the resolved invocation so a test
    can tell two rotating adapters apart without configuring a models table; the effort
    half always comes from the settings layer, exactly as a real adapter's does.
    """

    def __init__(self, steps, resolved_model=None):
        self.steps = list(steps)
        self.calls: list[tuple[str, str, str | None]] = []
        self.resolved_model = resolved_model
        self.resolve_calls: list[str] = []

    def resolve(self, model):
        self.resolve_calls.append(model)
        requested_model, requested_effort = super().resolve(model)
        return (self.resolved_model or requested_model, requested_effort)

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
        (self.plan_dir / "_runtime_test.toml").write_text(
            'execution = "disabled"\n', encoding="utf-8", newline="\n")
        # These tests exercise task-session scheduling and focused checkpoint
        # gates.  Full candidate verification has its own engine handoff tests.
        verifier = mock.patch(
            "assent.engine.verification.verify_plan_if_needed", return_value=0)
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

    def build(self, adapter_name="claude", extra_config=""):
        task_workflow = (
            "" if "[workflow]" in extra_config else
            '[workflow]\ntask = [{ action = "focused_test" }]\n')
        (self.root / ".assent" / "assent.toml").write_text(
            f'[adapter]\nname = "{adapter_name}"\n'
            '[adapter.claude]\ncommand = "python"\n'
            + extra_config
            + task_workflow
            + models_block(f'name = "{adapter_name}"\n' + extra_config),
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
