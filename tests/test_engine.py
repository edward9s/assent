"""engine tests: inject a ScriptedAdapter + fake sleep/now, and in a temporary repo verify
all branches of select task -> acceptance -> write-back. The real CLI is never touched.

Chinese literals that remain are deliberate user/upstream passthrough data (task titles,
notes, journal summaries, AGENTS.md content) used to prove that non-English data flows
through verbatim."""
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

from assent import AssentError, engine, gitops
from assent.adapters import Adapter, TaskResult
from assent.config import load_config
from assent.plan import append_entry, journal_path_for, parse_task_file, set_status

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
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.worktrees_root = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(shutil.rmtree, self.worktrees_root, ignore_errors=True)
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

    def build(self, retry=1, adapter_name="claude", prompt_template=None,
              extra_config=""):
        prompt = (f'[prompt]\ntemplate = {json.dumps(prompt_template)}\n'
                  if prompt_template is not None else "")
        (self.root / ".assent" / "assent.toml").write_text(
            f"[run]\nretry_per_task = {retry}\n"
            f'[adapter]\nname = "{adapter_name}"\n'
            '[adapter.claude]\ncommand = "python"\n'
            + extra_config
            + prompt,
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


class TestRunSuccess(EngineTestCase):
    def test_unfinished_folder_prerequisite_refuses_before_lock(self):
        self.write_task(1)
        base = self.root / ".assent" / "base"
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
                    "assent.engine.lockfile.hold_lock") as hold_lock:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    result = engine.run(cfg, adapter=adapter, **options)
                self.assertEqual(result, 1)
                self.assertIn(
                    "Prerequisite folder base still has 3 unfinished task(s)"
                    " (TODO 1, WIP 1, BLOCKED 1)", out.getvalue())
                hold_lock.assert_not_called()
        self.assertEqual(adapter.calls, [])

    def test_done_and_skip_folder_prerequisite_allows_run(self):
        path = self.write_task(1)
        base = self.root / ".assent" / "base"
        base.mkdir()
        for index, status in enumerate(("DONE", "SKIP"), 1):
            (base / f"t{index:03d}_task.e.toml").write_text(
                task_text(status=status), encoding="utf-8")
        (self.plan_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        base_worktree = gitops.worktree_path(self.root, "base")
        self._git("worktree", "add", "-b", "base/run",
                  str(base_worktree), "HEAD")
        (base_worktree / "base.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"], cwd=base_worktree,
            capture_output=True, encoding="utf-8", check=True)
        subprocess.run(
            ["git", "commit", "-m", "finish base"], cwd=base_worktree,
            capture_output=True, encoding="utf-8", check=True)
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
        # working tree clean apart from _report.md and assent.lock (runtime artifacts)
        porcelain = [ln for ln in self._git("status", "--porcelain").splitlines()
                     if ln.strip() and "_report.md" not in ln
                     and "assent.lock" not in ln]
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
                engine, "write_report", side_effect=PermissionError("report file locked")):
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
        cfg = self.build(extra_config=
            '[adapter.claude.default_effort]\nlite = "high"\n'
            '[adapter.claude.efforts]\nlow = "minimal"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][2], "minimal")

    def test_effort_default_applied_per_tier(self):
        p1 = self.write_task(1, model="lite")  # built-in lite default is medium
        cfg = self.build(extra_config=
            '[adapter.claude.efforts]\nmedium = "balanced"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0],
                         (adapter.calls[0][0], "lite", "balanced"))

    def test_effort_omitted_without_default_is_not_sent(self):
        p1 = self.write_task(1, model="lite")
        cfg = self.build(extra_config=
            '[adapter.claude.default_effort]\n'
            '[adapter.claude.efforts]\nlow = "minimal"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run(cfg, once=True, adapter=adapter)
        self.assertIsNone(adapter.calls[0][2])
        self.assertIn("effort(abstract)=unspecified", out.getvalue())
        self.assertIn("requested_effort(actual)=CLI default", out.getvalue())

    def test_effort_translation_uses_tier_then_flat_then_identity(self):
        cfg = self.build(extra_config=
            '[adapter.claude.efforts]\n'
            'low = "minimal"\nmedium = "balanced"\n'
            '[adapter.claude.efforts.lite]\nlow = "tiny"\n')
        self.assertEqual(engine._resolve_requested_effort(cfg, "lite", "low"),
                         "tiny")
        self.assertEqual(engine._resolve_requested_effort(
            cfg, "lite", "medium"), "balanced")
        self.assertEqual(engine._resolve_requested_effort(cfg, "lite", "high"),
                         "high")
        self.assertEqual(engine._resolve_requested_effort(cfg, "core", "low"),
                         "minimal")
        self.assertEqual(engine._resolve_requested_effort(cfg, "core", "high"),
                         "high")

        tier_only = self.build(extra_config=
            '[adapter.claude.efforts.lite]\nhigh = "max"\n')
        self.assertEqual(engine._resolve_requested_effort(
            tier_only, "lite", "high"), "max")
        self.assertEqual(engine._resolve_requested_effort(
            tier_only, "lite", "low"), "low")
        self.assertEqual(engine._resolve_requested_effort(
            tier_only, "core", "high"), "high")

    def test_codex_uses_its_own_effort_translation(self):
        p1 = self.write_task(1, model="lite", effort="high")
        cfg = self.build(adapter_name="codex", extra_config=
            '[adapter.claude.efforts]\nhigh = "claude-value"\n'
            '[adapter.codex.efforts.lite]\nhigh = "max"\n')
        self.commit_all()
        adapter = ScriptedAdapter([
            self.ai_done(p1, by="codex", requested_model="gpt-cli")],
            resolved_model="gpt-cli")
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][2], "max")

    def test_prompt_contains_task_and_journal_paths(self):
        p1 = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        prompt = adapter.calls[0][0]
        self.assertIn(str(cfg.assent_dir / "instructions.md"), prompt)
        self.assertIn(str(p1), prompt)
        self.assertIn(str(p1.with_name("t001_task.r.toml")), prompt)
        self.assertIn(_OK, prompt)
        self.assertIn('by = "claude"', prompt)
        self.assertIn('requested_model = "lite"', prompt)
        self.assertIn('abstract effort = "medium"', prompt)
        self.assertIn('requested_effort = "medium"', prompt)

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
            prompt_template=("{agent}|{requested_model}|{effort}|"
                             "{requested_effort}|{task_id}"))
        self.commit_all()
        adapter = ScriptedAdapter(
            [self.ai_done(p1, requested_model="cli-model")],
            resolved_model="cli-model")
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][0],
                         "claude|cli-model|medium|medium|t001")

    def test_session_output_distinguishes_abstract_and_requested_effort(self):
        p1 = self.write_task(1, model="lite", effort="high")
        cfg = self.build(extra_config=
            '[adapter.claude.efforts.lite]\nhigh = "max"\n')
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run(cfg, once=True,
                       adapter=ScriptedAdapter([self.ai_done(p1)]))
        self.assertIn("effort(abstract)=high", out.getvalue())
        self.assertIn("requested_effort(actual)=max", out.getvalue())

    def test_worktree_default_verify_uses_main_script_and_worktree_cwd(self):
        cfg = self.build()
        worktree = self.root / "isolated"
        worktree.mkdir()
        (cfg.assent_dir / "verify.py").write_text(
            "from pathlib import Path\n"
            "Path('verified.txt').write_text('ok', encoding='utf-8')\n",
            encoding="utf-8")

        self.assertEqual(engine._run_verify(
            cfg.for_worktree(worktree), "python .assent/verify.py"), 0)
        self.assertEqual((worktree / "verified.txt").read_text(encoding="utf-8"),
                         "ok")
        self.assertFalse((self.root / "verified.txt").exists())


class TestInvocationResolution(EngineTestCase):
    def test_resolved_effort_is_consistent_across_prompt_call_label_journal(self):
        # One resolved abstract/concrete pair must appear identically in the prompt
        # placeholders, the adapter call, the terminal label, and the scheduler journal.
        p1 = self.write_task(1, model="lite", effort="high")
        cfg = self.build(extra_config=
            '[adapter.claude.efforts.lite]\nhigh = "max"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run(cfg, once=True, adapter=adapter)

        prompt, requested_model, requested_effort = adapter.calls[0]
        self.assertEqual(requested_model, "lite")
        self.assertEqual(requested_effort, "max")            # concrete CLI value
        self.assertIn('abstract effort = "high"', prompt)    # abstract kept distinct
        self.assertIn('requested_effort = "max"', prompt)
        self.assertIn("effort(abstract)=high", out.getvalue())
        self.assertIn("requested_effort(actual)=max", out.getvalue())

        from assent.plan import read_entries
        done = next(e for e in read_entries(journal_path_for(p1))
                    if e["by"] == "claude")
        self.assertEqual(done["requested_model"], "lite")

    def test_unknown_adapter_run_is_rejected_without_claude_fallback(self):
        self.write_task(1)
        cfg = self.build(adapter_name="nowhere")
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg)   # no injected adapter -> get_adapter must refuse
        self.assertEqual(rc, 1)
        self.assertIn("unknown adapter: 'nowhere'", out.getvalue())
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())

    def test_unknown_adapter_check_reports_fail_and_skips_cli_probe(self):
        self.write_task(1)
        cfg = self.build(adapter_name="nowhere")
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.check(cfg)
        self.assertEqual(rc, 1)
        self.assertIn("adapter: FAIL", out.getvalue())
        self.assertIn("unknown adapter: 'nowhere'", out.getvalue())
        # the CLI probe is adapter-provided, so an unresolved adapter emits no CLI line
        self.assertNotIn("CLI:", out.getvalue())
        self.assertNotIn("capability preflight", out.getvalue())

    def test_check_cli_probe_uses_current_adapter_command(self):
        # codex adapter with a runnable command (python) must be probed as codex, not claude
        self.write_task(1)
        cfg = self.build(adapter_name="codex", extra_config=
            '[adapter.codex]\ncommand = "python"\n')
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 0)
        self.assertIn("codex CLI: OK", out.getvalue())

    def test_check_cli_probe_reports_missing_executable(self):
        self.write_task(1)
        cfg = self.build(adapter_name="codex", extra_config=
            '[adapter.codex]\ncommand = "definitely-not-a-real-cli-xyz"\n')
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 1)
        self.assertIn("codex CLI: FAIL (executable not found", out.getvalue())


class TestAntigravityCapabilityPreflight(EngineTestCase):
    """The active adapter proves every planned invocation before anything is spent.

    Antigravity is the adapter that actually publishes a capability catalog, so it is the one
    that exercises the shared gate; the adapters without one keep passing it trivially.
    """

    BAD_PRO_MEDIUM = ('[adapter]\nname = "antigravity"\n'
                      '[adapter.antigravity.efforts.prime]\nmedium = "medium"\n')

    def setUp(self):
        super().setUp()
        from assent.adapters import antigravity
        self.catalog = antigravity.parse_models_catalog(
            (Path(__file__).resolve().parent / "fixtures"
             / "agy_models_1.1.5.txt").read_text(encoding="utf-8"))
        # Listing models costs nothing, but no test may reach a real installation.
        catalog_patch = mock.patch.object(
            antigravity, "load_catalog", return_value=self.catalog)
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        # Any attempt to open an actual AGY session is a test failure.
        session_patch = mock.patch.object(
            antigravity, "run_subprocess",
            side_effect=AssertionError("no AGY session may be started"))
        self.session = session_patch.start()
        self.addCleanup(session_patch.stop)

    def antigravity_cfg(self, extra_config=BAD_PRO_MEDIUM):
        (self.root / ".assent" / "assent.toml").write_text(
            extra_config, encoding="utf-8")
        return load_config(self.root / ".assent" / "assent.toml", "plan01")

    def test_run_refuses_pro_medium_before_session_status_or_git_change(self):
        path = self.write_task(1, model="prime", effort="medium")
        cfg = self.antigravity_cfg()
        self.commit_all()
        commits_before = self._git("log", "--pretty=%H")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True), 1)

        text = out.getvalue()
        self.assertIn("antigravity capability preflight: FAIL", text)
        self.assertIn("--model gemini-3.1-pro --effort medium", text)
        self.assertIn("available: low, high", text)
        self.assertIn('[adapter.antigravity.efforts.prime] medium = "high"', text)
        # nothing was started, marked, journalled or committed
        self.session.assert_not_called()
        self.assertEqual(parse_task_file(path).status, "TODO")
        self.assertFalse(journal_path_for(path).exists())
        self.assertEqual(self._git("log", "--pretty=%H"), commits_before)
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())
        self.assertEqual(gitops.branches_with_prefix(self.root, "plan01/"), [])

    def test_check_refuses_the_same_mapping_with_the_same_diagnostic(self):
        self.write_task(1, model="prime", effort="medium")
        cfg = self.antigravity_cfg()
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 1)

        text = out.getvalue()
        self.assertIn("antigravity capability preflight: FAIL", text)
        self.assertIn('[adapter.antigravity.efforts.prime] medium = "high"', text)
        self.session.assert_not_called()

    def test_shipped_mapping_passes_the_preflight_for_every_tier(self):
        for num, tier in enumerate(("prime", "core", "lite"), start=1):
            self.write_task(num, model=tier)
        cfg = self.antigravity_cfg('[adapter]\nname = "antigravity"\n')
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.check(cfg)
        self.assertIn("antigravity capability preflight: OK", out.getvalue())

    def test_settled_tasks_do_not_gate_a_run_they_cannot_join(self):
        self.write_task(1, model="prime", effort="medium", status="DONE")
        path = self.write_task(2, model="core")
        cfg = self.antigravity_cfg()
        self.commit_all()

        from assent.adapters.antigravity import AntigravityAdapter
        adapter = AntigravityAdapter(cfg, catalog=self.catalog)
        adapter.run_task = lambda prompt, model, effort, cwd: (
            set_status(path, "DONE") or ok_result())

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestAntigravitySession(EngineTestCase):
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
        path = self.write_task(1, model="prime", effort="low")
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


class TestAcceptanceGates(EngineTestCase):
    def test_self_blocked_committed_without_verify(self):
        # verify is an always-failing command: self-marked BLOCKED skips verify so it passes
        # (verify only runs when there is an implementation)
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
        self.assertEqual(parse_task_file(path).status, "BLOCKED")  # marked by scheduler

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
        self.assertIn("Reason:", adapter.calls[1][0])       # retry prompt carries the failure reason
        self.assertIn("outside.py", self._git_execution(
            "ls-files"))  # output not discarded, gathered into the checkpoint
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        blocked = next(e for e in entries if e["by"] == "scheduler"
                       and e["event"] == "blocked")
        self.assertEqual(blocked["agent"], "claude")
        self.assertEqual(blocked["requested_model"], "lite")
        self.assertEqual(blocked["requested_effort"], "medium")

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

    def test_tampered_self_blocked_task_file_retries_then_scheduler_blocks(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def tamper(prompt):
            # execution AI loosens its own scope + verify, and self-marks BLOCKED
            path.write_text(task_text(status="BLOCKED", scope=("src/", "secret/"),
                                      verify="echo ok"),
                            encoding="utf-8", newline="\n")
            return ok_result()

        self.run_quiet(cfg, once=True, adapter=ScriptedAdapter([tamper]))
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any("fields other than status" in e["summary"]
                            for e in entries if e["by"] == "scheduler"))
        self.assertFalse(any(
            subject == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for subject in self.subjects()))

    def test_malformed_self_blocked_task_retries_before_handoff(self):
        path = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        def malformed(prompt):
            path.write_text('status = "BLOCKED"\nnot valid toml = [\n',
                            encoding="utf-8", newline="\n")
            return ok_result()

        def repaired(prompt):
            self.assertIn("Re-parsing the task file failed", prompt)
            path.write_text(task_text(status="BLOCKED"), encoding="utf-8",
                            newline="\n")
            return ok_result()

        adapter = ScriptedAdapter([malformed, repaired])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertFalse(journal_path_for(path).exists())

    def test_self_blocked_checks_scope_across_wip_checkpoint(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def quota_with_rogue_file(prompt):
            (self.execution_root() / "rogue.py").write_text(
                "kept", encoding="utf-8")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def blocked(prompt):
            set_status(path, "BLOCKED")
            return ok_result()

        self.assertEqual(self.run_quiet(
            cfg, once=True, sleep=lambda _: None,
            adapter=ScriptedAdapter([quota_with_rogue_file, blocked])), 0)
        self.assertFalse(any(
            subject == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for subject in self.subjects()))
        from assent.plan import read_entries
        scheduler_blocked = next(
            entry for entry in read_entries(journal_path_for(path))
            if entry["by"] == "scheduler" and entry["event"] == "blocked")
        self.assertIn("Changes outside scope appeared: rogue.py",
                      scheduler_blocked["summary"])

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
            s == "auto(plan01/t001): BLOCKED (execution AI self-marked)"
            for s in self.subjects()))


class TestAdapterProcessOutcomes(EngineTestCase):
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


class TestBillingAbort(EngineTestCase):
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
        self.assertAlmostEqual(sum(sleeps), (5 + 2) * 60, delta=1)  # +2 minute buffer
        self.assertIn("resume", adapter.calls[1][0])
        # one zero-token capability preflight before the run, then one per attempt
        self.assertEqual(adapter.resolve_calls, ["lite", "lite", "lite"])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in subjects))
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "lite")
        self.assertEqual(quota["requested_effort"], "medium")
        self.assertNotIn("session", [e["event"] for e in entries])

    def test_wip_task_resumed_on_startup(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
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


class TestCrashDirtyWorktreeRecovery(EngineTestCase):
    """Startup recovery of a worktree left dirty by an unclean process exit (hard power loss /
    kill) that never reached the Ctrl+C / quota / infrastructure interrupt handlers."""

    def _reused_worktree(self):
        """A worktree that a prior run created and left behind, on the folder's work branch."""
        worktree = gitops.ensure_worktree(self.root, "plan01")
        gitops.ensure_branch(worktree, "plan01/")
        return worktree

    def test_scope_attributable_dirty_worktree_recovers_and_resumes(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "partial.py").write_text("wip", encoding="utf-8")

        adapter = ScriptedAdapter([self.ai_done(path)])
        rc = self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(rc, 0)
        # The crash progress is gathered into a wip checkpoint before any session.
        self.assertTrue(
            any(s.startswith("wip(plan01/t001): recovered dirty worktree")
                for s in self.subjects()))
        # Recovery opens no session of its own; the candidate then resumes exactly
        # once with a continue prompt -- no retry counter is consumed.
        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")
        # A scheduler recovery entry, with no fabricated AI session identity.
        from assent.plan import read_entries
        recovery = next(
            e for e in read_entries(journal_path_for(path))
            if e["by"] == "scheduler" and e["event"] == "interrupt"
            and "Recovered a dirty worktree" in e["summary"])
        self.assertNotIn("agent", recovery)
        self.assertNotIn("requested_model", recovery)
        # The recovered partial work survived into the tree, never discarded.
        self.assertTrue((worktree / "src" / "partial.py").is_file())

    def test_changes_outside_candidate_scope_stay_fail_closed(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "outside.txt").write_text("stray", encoding="utf-8")

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 1)
        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(path).status, "TODO")
        self.assertFalse(any(s.startswith("wip(plan01/t001)")
                             for s in self.subjects()))
        self.assertFalse(journal_path_for(path).exists())

    def test_no_resumable_candidate_stays_fail_closed(self):
        path = self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "stray.py").write_text("x", encoding="utf-8")

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 1)
        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(any(s.startswith("wip(plan01/") for s in self.subjects()))


class TestSchedulingAndRefusals(EngineTestCase):
    def test_run_and_check_refuse_root_without_own_git_marker(self):
        nested_root = self.root / "not-repo"
        nested_plan = nested_root / ".assent" / "plan01"
        nested_plan.mkdir(parents=True)
        config = nested_root / ".assent" / "assent.toml"
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
                    "This project has no git repository yet; run git init first",
                    out.getvalue())

        self.assertEqual(adapter.calls, [])
        self.assertFalse((nested_plan / "assent.lock").exists())

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
        self.assertEqual(parse_task_file(p2).status, "TODO")   # blocked by prerequisite
        self.assertEqual(parse_task_file(p3).status, "DONE")   # no deps, runs anyway

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
        self.assertIn("retired legacy task files", check_out.getvalue())
        self.assertIn("move them", check_out.getvalue())

        run_out = io.StringIO()
        with contextlib.redirect_stdout(run_out):
            self.assertEqual(engine.run(cfg, adapter=adapter), 1)
        self.assertIn("retired legacy task files", run_out.getvalue())
        self.assertIn("move them", run_out.getvalue())
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
        engine._countdown(90, "Quota reset", sleeps.append, stream=stream)
        self.assertEqual(sleeps, [90])
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertNotIn("\r", stream.getvalue())

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
        self.commit_all()  # claude command = python, so --version is always runnable
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.check(cfg), 0)
        self.assertIn("Result: passed", out.getvalue())

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
        self.assertIn("Folder dependencies: FAIL", out.getvalue())
        self.assertIn("unknown keys", out.getvalue())

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

        from assent.plan import Plan
        text = engine.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("t001  DONE", text)
        self.assertIn("t002  BLOCKED", text)
        self.assertIn("last journal (scheduler)", text)
        self.assertIn("[", text)  # DONE task carries a checkpoint hash
        # _report.md written out, but not version-controlled
        self.assertTrue((cfg.tasks_dir / "_report.md").is_file())
        self.assertNotIn("_report.md", self._git_execution("ls-files"))

    def test_report_isolates_namespaced_checkpoints(self):
        self.write_task(1, status="DONE", title="目前一")
        self.write_task(3, status="DONE", title="目前三")
        other_dir = self.root / ".assent" / "plan010"
        other_dir.mkdir()
        (other_dir / "t001_other.e.toml").write_text(
            task_text(status="DONE", title="其他一"), encoding="utf-8",
            newline="\n")
        (other_dir / "t003_other.e.toml").write_text(
            task_text(status="DONE", title="其他三"), encoding="utf-8",
            newline="\n")
        cfg = self.build()
        other_cfg = load_config(
            self.root / ".assent" / "assent.toml", folder="plan010")
        self.commit_all()

        def checkpoint(subject):
            self._git("commit", "--allow-empty", "-m", subject)
            return self._git("rev-parse", "--short", "HEAD").strip()

        other_t1 = checkpoint("auto(plan010/t001): 其他一")
        other_t3 = checkpoint("auto(plan010/t003): 其他三")
        legacy_t3 = checkpoint("auto(t003): legacy format, ownership unclear")
        wrong_id = checkpoint("auto(plan01/t0010): task id is only a prefix")
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
        self.assertIn("Progress: DONE 2 / BLOCKED 0 / WIP 0 / TODO 0 / SKIP 0 (2 total)",
                      current)

    def test_report_reads_legacy_ai_entry_without_identity_fields(self):
        path = self.write_task(1, status="BLOCKED")
        journal_path_for(path).write_text(
            '[[entry]]\ntime = "2026-07-17T00:00:00+00:00"\n'
            'by = "ai"\nevent = "blocked"\nsummary = "舊日誌仍可讀"\n',
            encoding="utf-8")
        cfg = self.build()
        from assent.plan import Plan
        text = engine.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("last journal (ai): 舊日誌仍可讀", text)


class TestStackReportLines(EngineTestCase):
    """A complete folder (all DONE/SKIP) must skip stack resolution entirely;
    an incomplete folder must keep today's three existing outputs verbatim."""

    def test_complete_folder_skips_resolution_and_reports_not_applicable(self):
        self.write_task(1, status="DONE")
        self.write_task(2, slug="skip", status="SKIP", title="略過")
        cfg = self.build()
        self.commit_all()
        from assent.plan import Plan
        plan = Plan.parse(cfg.tasks_dir)
        with mock.patch(
                "assent.engine._resolve_stack_state",
                side_effect=AssertionError(
                    "must not resolve stack state for a complete folder")):
            lines = engine._stack_report_lines(cfg, plan)
        self.assertEqual(
            lines, ["Stack base: not applicable (folder complete)"])

    def test_incomplete_folder_still_reports_current_target_main(self):
        self.write_task(1)  # TODO, no upstream declared
        cfg = self.build()
        self.commit_all()
        from assent.plan import Plan
        plan = Plan.parse(cfg.tasks_dir)
        lines = engine._stack_report_lines(cfg, plan)
        self.assertEqual(lines, [
            "Stack base: current target main",
            "Speculative upstream: none (all direct upstreams accepted)"])

    def test_incomplete_folder_still_reports_unavailable_on_resolution_error(self):
        self.write_task(1)  # TODO
        cfg = self.build()
        self.commit_all()
        from assent.plan import Plan
        plan = Plan.parse(cfg.tasks_dir)
        with mock.patch(
                "assent.engine._resolve_stack_state",
                side_effect=AssentError(
                    "upstream folder plan00 has no plan00/* source branch")):
            lines = engine._stack_report_lines(cfg, plan)
        self.assertEqual(lines, [
            "Stack base: unavailable (upstream folder plan00 has no "
            "plan00/* source branch)"])

    def test_incomplete_folder_still_reports_stacked_speculative_upstream(self):
        self.write_task(1)  # TODO
        cfg = self.build()
        self.commit_all()
        from assent.plan import Plan
        from assent.folderdeps import FolderBaseResolution
        plan = Plan.parse(cfg.tasks_dir)
        upstream = gitops.FolderSourceSnapshot(
            folder="plan00", branch="plan00/run", worktree=self.root,
            tip="abc123")
        state = engine._StackState(
            base=FolderBaseResolution(
                target_snapshot="deadbeef", speculative_upstream=upstream,
                resolved_base="abc123"),
            sources=(upstream,))
        with mock.patch(
                "assent.engine._resolve_stack_state", return_value=state):
            lines = engine._stack_report_lines(cfg, plan)
        self.assertEqual(lines, [
            "Stack base: abc123",
            "Speculative upstream: plan00 @ abc123 (unaccepted)"])

    def test_status_and_report_show_not_applicable_for_complete_folder(self):
        self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.status(cfg), 0)
        self.assertIn(
            "Stack base: not applicable (folder complete)", out.getvalue())
        from assent.plan import Plan
        text = engine.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn(
            "Stack base: not applicable (folder complete)", text)


class TestReworkPromptSuffix(EngineTestCase):
    """A TODO task whose journal's last entry is a pending rework_requested record gets a
    prompt suffix carrying the rejection reason, so the execution AI does not mistake the
    rejected implementation/tests for the current spec."""

    def test_rework_requested_last_entry_carries_reason_into_prompt(self):
        path = self.write_task(1)
        append_entry(
            journal_path_for(path), by="scheduler", event="rework_requested",
            summary="Manual rework requested; scheduler reset status DONE back to TODO",
            detail=("target id: t001\noriginal status: DONE\nHEAD: deadbeef\n"
                    "cascade scope: disabled\n"
                    "reason: the parser drops trailing whitespace"))
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        prompt = adapter.calls[0][0]
        self.assertIn("rejected by a human reviewer", prompt)
        self.assertIn("the parser drops trailing whitespace", prompt)
        self.assertIn("do not assume the existing code or existing tests are correct", prompt)

    def test_last_entry_not_rework_requested_adds_no_suffix(self):
        path = self.write_task(1)
        append_entry(
            journal_path_for(path), by="scheduler", event="blocked",
            summary="Still failed acceptance after 1 retries")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        prompt = adapter.calls[0][0]
        self.assertNotIn("rejected by a human reviewer", prompt)

    def test_missing_journal_adds_no_suffix_and_does_not_raise(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])

        self.assertFalse(journal_path_for(path).exists())
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        prompt = adapter.calls[0][0]
        self.assertNotIn("rejected by a human reviewer", prompt)

    def test_unparseable_journal_adds_no_suffix_and_does_not_raise(self):
        path = self.write_task(1)
        journal_path_for(path).write_text("not [valid toml", encoding="utf-8")
        cfg = self.build()
        self.commit_all()

        # This step avoids append_entry: the pre-existing broken journal is the very
        # condition under test, and append_entry re-parsing it is a separate concern.
        def step(prompt):
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([step])

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        prompt = adapter.calls[0][0]
        self.assertNotIn("rejected by a human reviewer", prompt)


if __name__ == "__main__":
    unittest.main()
