"""End-to-end walkthrough: temporary repo + task folder + scriptable fake adapter, a set of
scenario integration tests.

Stands on its own test scaffolding (does not import other test_*.py across files), matching
the convention of the other test files.

Chinese literals that remain are deliberate user/upstream passthrough data (task titles,
goals, journal summaries, AGENTS.md/instructions/format content) used to prove that
non-English data flows through verbatim.
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
from unittest import mock

from assent import AssentError, engine
from assent import gitops
from assent.adapters import Adapter, TaskResult
from assent.config import load_config
from assent.lockfile import hold_lock
from assent.plan import (append_entry, journal_path_for, parse_task_file,
                         set_status)

_OK = 'python -c "raise SystemExit(0)"'
_WORKTREE_GITIGNORE = ".assent/\n"


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
        self.cwds = []

    def run_task(self, prompt, model, effort, cwd):
        self.calls.append(prompt)
        self.cwds.append(Path(cwd))
        step = self.steps.pop(0)
        return step(prompt) if callable(step) else step


class E2ETestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.worktrees_root = self.root.parent / f"{self.root.name}.worktrees"
        self.addCleanup(shutil.rmtree, self.worktrees_root, ignore_errors=True)
        self._git("init")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(
            _WORKTREE_GITIGNORE, encoding="utf-8")
        self.plan_dir = self.root / ".assent" / "plan01"
        self.plan_dir.mkdir(parents=True)
        (self.root / ".assent" / "assent.toml").write_text(
            '[run]\nretry_per_task = 1\n',
            encoding="utf-8")
        (self.root / "AGENTS.md").write_text("專案規則\n", encoding="utf-8")
        (self.root / ".assent" / "instructions.md").write_text(
            "assent 工作指示\n", encoding="utf-8")
        (self.root / ".assent" / "format.md").write_text(
            "計畫格式\n", encoding="utf-8")
        (self.root / ".assent" / "verify.py").write_text(
            "from pathlib import Path\n"
            "root = Path.cwd()\n"
            "ok = not (root / '.assent').exists()\n"
            "raise SystemExit(0 if ok else 1)\n",
            encoding="utf-8")

    def _git(self, *args) -> str:
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              encoding="utf-8", check=True).stdout

    def cfg(self):
        return load_config(self.root / ".assent" / "assent.toml", "plan01")

    def add_task(self, num, **kw) -> Path:
        path = self.plan_dir / f"t{num:03d}_task.e.toml"
        path.write_text(task_text(**kw), encoding="utf-8", newline="\n")
        return path

    def start(self):
        self._git("add", "-A")
        self._git("commit", "-m", "init")

    def execution_root(self, folder="plan01") -> Path:
        candidate = gitops.worktree_path(self.root, folder)
        return candidate if candidate.exists() else self.root

    def done_step(self, path, files=None):
        def step(prompt):
            for rel, content in (files or {}).items():
                p = self.execution_root() / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content, encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="done", summary="完成")
            return ok_result()
        return step

    def run_engine(self, adapter, **kw) -> int:
        # Folder-final verification has separate authority tests in
        # tests.test_verification_engine.  These scenarios exercise task and
        # worktree behavior only, so avoid imposing candidate-source fixtures.
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch("assent.engine.verification.verify_folder_if_needed",
                           return_value=0):
            return engine.run(self.cfg(), adapter=adapter, **kw)

    def subjects(self):
        return self.git_at(self.execution_root(), "log", "--pretty=%s").splitlines()

    def git_at(self, root, *args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True,
                              encoding="utf-8", check=True).stdout


class TestScenarios(E2ETestCase):
    def test_task_runs_end_to_end_with_matching_journal(self):
        task = self.add_task(1)
        self.start()
        adapter = ScriptedAdapter([
            self.done_step(task, {"src/formal.py": "ok"})])

        self.assertEqual(self.run_engine(adapter, once=True), 0)

        journal = task.with_name("t001_task.r.toml")
        self.assertEqual(parse_task_file(task).status, "DONE")
        self.assertTrue(journal.is_file())
        self.assertFalse(task.with_name("t001_task.e.r.toml").exists())
        self.assertIn(str(task), adapter.calls[0])
        self.assertIn(str(journal), adapter.calls[0])
        report = engine.render_report(
            self.cfg(), engine.Plan.parse(self.plan_dir))
        self.assertIn("t001  DONE", report)

    def test_smooth_run_three_tasks(self):
        """Smooth scenario: three tasks reach DONE in order, three checkpoints, clean tree."""
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
                     if ln.strip() and "_report.md" not in ln
                     and "assent.lock" not in ln]
        self.assertEqual(porcelain, [])

    def test_fail_retry_then_pass(self):
        """Fail-then-retry scenario: first round leaves an out-of-scope file -> the retry
        prompt carries the reason -> the second round fixes it and passes. Output is never
        discarded: the out-of-scope file ends up in the checkpoint (the second round moves
        it into scope)."""
        p1 = self.add_task(1, scope=("src/", "outside.py"))
        self.start()

        def first(prompt):
            (self.execution_root() / "outside_tmp.py").write_text(
                "x", encoding="utf-8")
            set_status(p1, "DONE")
            return ok_result()

        def second(prompt):
            # fix: rename the out-of-scope file to a name inside scope
            root = self.execution_root()
            (root / "outside_tmp.py").rename(root / "outside.py")
            return ok_result()

        adapter = ScriptedAdapter([first, second])
        self.assertEqual(self.run_engine(adapter, once=True), 0)
        self.assertIn("Reason:", adapter.calls[1])
        self.assertEqual(parse_task_file(p1).status, "DONE")
        self.assertIn("outside.py", self.git_at(
            self.execution_root(), "ls-files"))

    def test_nonzero_done_keeps_work_and_retries_before_checkpoint(self):
        """A DONE status from a failed adapter process is not accepted until a clean retry."""
        p1 = self.add_task(1)
        self.start()

        def nonzero_done(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "kept.py").write_text("kept", encoding="utf-8")
            set_status(p1, "DONE")
            return TaskResult(exit_code=6, output="transport ended early",
                              quota_exhausted=False, reset_at=None)

        def clean_retry(prompt):
            self.assertIn("exit code 6", prompt)
            self.assertFalse(any(
                s.startswith("auto(plan01/t001): ") for s in self.subjects()))
            return ok_result()

        adapter = ScriptedAdapter([nonzero_done, clean_retry])
        self.assertEqual(self.run_engine(adapter, once=True), 0)
        self.assertEqual(parse_task_file(p1).status, "DONE")
        self.assertIn("src/kept.py", self.git_at(
            self.execution_root(), "ls-files"))
        from assent.plan import read_entries
        failure = next(e for e in read_entries(journal_path_for(p1))
                       if e["event"] == "adapter_exit")
        self.assertEqual(failure["exit_code"], 6)

    def test_blocked_gates_downstream_others_proceed(self):
        """BLOCKED scenario: t001 exhausts retries -> scheduler marks BLOCKED + records in the
        r file; t002 depending on it is blocked, and t003 with no deps runs anyway, all within
        the same run."""
        p1 = self.add_task(1)
        p2 = self.add_task(2, deps=("t001",))
        p3 = self.add_task(3)
        self.start()

        def bad(prompt):
            (self.execution_root() / "rogue.py").write_text(
                "x", encoding="utf-8")
            set_status(p1, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([bad, lambda p: ok_result(),
                                   self.done_step(p3)])
        self.assertEqual(self.run_engine(adapter), 0)
        self.assertEqual(parse_task_file(p1).status, "BLOCKED")
        self.assertEqual(parse_task_file(p2).status, "TODO")
        self.assertEqual(parse_task_file(p3).status, "DONE")
        from assent.plan import read_entries
        self.assertTrue(any(e["event"] == "blocked"
                            for e in read_entries(journal_path_for(p1))))

    def test_quota_interrupt_wip_then_resume(self):
        """Quota scenario: first round exhausts quota -> wip checkpoint + r-file quota record
        -> fake clock waits 5+2 minutes -> reruns the same task with a resume prompt and
        succeeds."""
        p1 = self.add_task(1)
        self.start()
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        sleeps: list[float] = []

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "half.py").write_text("h", encoding="utf-8")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=t0 + timedelta(minutes=5))

        adapter = ScriptedAdapter([quota_step, self.done_step(p1)])
        rc = self.run_engine(adapter, sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), 420, delta=1)
        self.assertIn("resume", adapter.calls[1])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in subjects))
        self.assertEqual(parse_task_file(p1).status, "DONE")
        from assent.plan import read_entries
        events = [e["event"] for e in read_entries(journal_path_for(p1))]
        self.assertNotIn("session", events)


class TestWorktreeScenarios(E2ETestCase):
    def configure_git_run(self):
        (self.root / ".gitignore").write_text(
            _WORKTREE_GITIGNORE, encoding="utf-8")
        (self.root / ".assent" / "assent.toml").write_text(
            '[run]\nretry_per_task = 1\n', encoding="utf-8")

    def isolated_done_step(self, adapter, path, files):
        def step(prompt):
            cwd = adapter.cwds[-1]
            for rel, content in files.items():
                target = cwd / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="done", summary="完成")
            return ok_result()
        return step

    def make_source(self, folder, start="HEAD"):
        worktree = gitops.worktree_path(self.root, folder)
        self._git("worktree", "add", "-b", f"{folder}/run",
                  str(worktree), start)
        (worktree / f"{folder}.txt").write_text(
            f"{folder}\n", encoding="utf-8")
        self.git_at(worktree, "add", "-A")
        self.git_at(worktree, "commit", "-m", f"finish {folder}")
        return worktree, self.git_at(
            worktree, "rev-parse", "HEAD").strip()

    def add_upstream_dependency(self, folder="upstream"):
        upstream = self.root / ".assent" / folder
        upstream.mkdir()
        (upstream / "t001_task.e.toml").write_text(
            task_text(status="DONE"), encoding="utf-8", newline="\n")
        (self.plan_dir / "_folder.toml").write_text(
            f'after = ["{folder}"]\n', encoding="utf-8")

    def test_run_isolated_from_main_tree_and_queries_use_worktree(self):
        self.configure_git_run()
        verify = ('python -c "import pathlib,sys;sys.exit(0 if '
                  "pathlib.Path('src/ok.txt').is_file() else 1)\"")
        task = self.add_task(1, verify=verify)
        self.start()
        main_branch = self._git("branch", "--show-current").strip()
        main_head = self._git("rev-parse", "HEAD").strip()
        adapter = ScriptedAdapter([])
        adapter.steps.append(self.isolated_done_step(
            adapter, task, {"src/ok.txt": "ok"}))

        self.assertEqual(self.run_engine(adapter, once=True), 0)

        cfg = self.cfg()
        worktree = gitops.worktree_path(self.root, "plan01")
        self.assertEqual(adapter.cwds, [worktree.resolve()])
        self.assertIn(str(task.resolve()), adapter.calls[0])
        self.assertIn(str((self.root / ".assent" / "instructions.md").resolve()),
                      adapter.calls[0])
        self.assertIn("project rules AGENTS.md", adapter.calls[0])
        self.assertNotIn(str((self.root / "AGENTS.md").resolve()),
                         adapter.calls[0])
        self.assertEqual(parse_task_file(task).status, "DONE")
        self.assertEqual(self._git("status", "--porcelain"), "")
        self.assertEqual(self._git("branch", "--show-current").strip(), main_branch)
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), main_head)
        worktree_branch = self.git_at(
            worktree, "branch", "--show-current").strip()
        self.assertTrue(worktree_branch.startswith("plan01/"))
        self.assertNotIn("auto(plan01/t001)",
                         self._git("log", "--pretty=%s"))
        self.assertIn("auto(plan01/t001)",
                      self.git_at(worktree, "log", "--pretty=%s"))

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.status(cfg), 0)
        self.assertIn(f"Current branch: {worktree_branch}", out.getvalue())
        self.assertIn("auto(plan01/t001)", out.getvalue())
        report = engine.render_report(cfg, engine.Plan.parse(cfg.tasks_dir))
        self.assertIn(f"Branch: {worktree_branch}", report)
        self.assertIn("t001  DONE", report)
        self.assertIn("[", report)

    def test_first_downstream_run_starts_from_unaccepted_upstream_tip(self):
        self.configure_git_run()
        task = self.add_task(1)
        self.add_upstream_dependency()
        self.start()
        _, upstream_tip = self.make_source("upstream")
        adapter = ScriptedAdapter([])

        def finish(prompt):
            self.assertTrue((adapter.cwds[-1] / "upstream.txt").is_file())
            return self.isolated_done_step(
                adapter, task, {"src/downstream.py": "ok"})(prompt)

        adapter.steps.append(finish)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch(
                "assent.engine.verification.verify_folder_if_needed",
                return_value=0):
            self.assertEqual(engine.run(
                self.cfg(), once=True, adapter=adapter), 0)

        downstream = gitops.worktree_path(self.root, "plan01")
        downstream_tip = self.git_at(downstream, "rev-parse", "HEAD").strip()
        self.assertTrue(gitops.is_ancestor(
            self.root, upstream_tip, downstream_tip))
        self.assertIn(f"Stacked upstream: upstream @ {upstream_tip}",
                      out.getvalue())
        self.assertNotIn("accepted", out.getvalue().lower())

    def test_accepted_unchanged_upstream_allows_existing_downstream_resume(self):
        self.configure_git_run()
        first = self.add_task(1)
        second = self.add_task(2, deps=("t001",))
        self.add_upstream_dependency()
        self.start()
        _, upstream_tip = self.make_source("upstream")
        adapter = ScriptedAdapter([])
        adapter.steps.append(self.isolated_done_step(
            adapter, first, {"src/first.py": "first"}))
        self.assertEqual(self.run_engine(adapter, once=True), 0)
        downstream = gitops.worktree_path(self.root, "plan01")
        branch_before = self.git_at(
            downstream, "branch", "--show-current").strip()

        self._git("merge", "--no-ff", "-m", "accept upstream", upstream_tip)
        resumed = ScriptedAdapter([])
        resumed.steps.append(self.isolated_done_step(
            resumed, second, {"src/second.py": "second"}))

        self.assertEqual(self.run_engine(resumed, once=True), 0)
        self.assertEqual(self.git_at(
            downstream, "branch", "--show-current").strip(), branch_before)
        self.assertTrue((downstream / "src" / "first.py").is_file())
        self.assertTrue((downstream / "src" / "second.py").is_file())

    def test_advanced_upstream_refuses_existing_downstream_without_changes(self):
        self.configure_git_run()
        self.add_task(1)
        self.add_upstream_dependency()
        self.start()
        upstream, old_tip = self.make_source("upstream")
        downstream = gitops.ensure_worktree(
            self.root, "plan01", old_tip)
        branch = gitops.ensure_branch(downstream, "plan01/")
        (downstream / "keep.txt").write_text("keep\n", encoding="utf-8")
        self.git_at(downstream, "add", "-A")
        self.git_at(downstream, "commit", "-m", "downstream work")
        downstream_tip = self.git_at(downstream, "rev-parse", "HEAD").strip()
        (upstream / "upstream.txt").write_text(
            "advanced\n", encoding="utf-8")
        self.git_at(upstream, "add", "-A")
        self.git_at(upstream, "commit", "-m", "advance upstream")
        adapter = ScriptedAdapter([])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                self.cfg(), once=True, adapter=adapter), 1)

        self.assertEqual(adapter.calls, [])
        self.assertIn("stale stack", out.getvalue())
        self.assertEqual(self.git_at(
            downstream, "branch", "--show-current").strip(), branch)
        self.assertEqual(self.git_at(
            downstream, "rev-parse", "HEAD").strip(), downstream_tip)
        self.assertEqual((downstream / "keep.txt").read_text(encoding="utf-8"),
                         "keep\n")
        self.assertEqual(self.git_at(downstream, "status", "--porcelain"), "")

    def test_upstream_creation_race_cleans_only_new_downstream_resources(self):
        self.configure_git_run()
        self.add_task(1)
        self.add_upstream_dependency()
        self.start()
        upstream, _ = self.make_source("upstream")
        real_resolve = engine._resolve_stack_state
        calls = 0

        def advance_before_second_resolution(cfg):
            nonlocal calls
            calls += 1
            if calls == 2:
                (upstream / "upstream.txt").write_text(
                    "raced\n", encoding="utf-8")
                self.git_at(upstream, "add", "-A")
                self.git_at(upstream, "commit", "-m", "race upstream")
            return real_resolve(cfg)

        adapter = ScriptedAdapter([])
        with mock.patch("assent.engine._resolve_stack_state",
                        side_effect=advance_before_second_resolution):
            self.assertEqual(self.run_engine(adapter, once=True), 1)

        self.assertEqual(adapter.calls, [])
        self.assertFalse(gitops.worktree_path(
            self.root, "plan01").exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, "plan01/"), [])
        self.assertTrue(upstream.exists())
        self.assertEqual(gitops.current_branch(upstream), "upstream/run")

    def test_branch_creation_race_keeps_foreign_ref_and_cleans_new_path(self):
        self.configure_git_run()
        self.add_task(1)
        self.start()
        collision = "plan01/collision"
        self._git("branch", collision, "HEAD")
        fake_datetime = mock.Mock()
        fake_datetime.now.return_value.strftime.return_value = "collision"
        adapter = ScriptedAdapter([])

        with mock.patch("assent.gitops.datetime", fake_datetime):
            self.assertEqual(self.run_engine(adapter, once=True), 1)

        self.assertEqual(adapter.calls, [])
        self.assertFalse(gitops.worktree_path(
            self.root, "plan01").exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, "plan01/"), [collision])

    def test_interrupt_during_new_setup_cleans_resources_before_returning_130(self):
        self.configure_git_run()
        self.add_task(1)
        self.start()
        real_resolve = engine._resolve_stack_state
        calls = 0

        def interrupt_second_resolution(cfg):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt
            return real_resolve(cfg)

        adapter = ScriptedAdapter([])
        with mock.patch("assent.engine._resolve_stack_state",
                        side_effect=interrupt_second_resolution):
            self.assertEqual(self.run_engine(adapter, once=True), 130)

        self.assertEqual(adapter.calls, [])
        self.assertFalse(gitops.worktree_path(
            self.root, "plan01").exists())
        self.assertEqual(gitops.branches_with_prefix(
            self.root, "plan01/"), [])

    def test_cleanup_failure_retains_recoverable_new_resources(self):
        self.configure_git_run()
        self.add_task(1)
        self.start()
        real_resolve = engine._resolve_stack_state
        calls = 0

        def fail_second_resolution(cfg):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AssentError("simulated race")
            return real_resolve(cfg)

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch(
                "assent.engine._resolve_stack_state",
                side_effect=fail_second_resolution), mock.patch(
                    "assent.gitops.cleanup_unstarted_worktree",
                    side_effect=AssentError("simulated cleanup failure")):
            self.assertEqual(engine.run(
                self.cfg(), once=True, adapter=adapter), 1)

        self.assertEqual(adapter.calls, [])
        self.assertTrue(gitops.worktree_path(
            self.root, "plan01").exists())
        self.assertIn("resources were retained for recovery", out.getvalue())

    def test_default_verify_script_runs_inside_worktree(self):
        self.configure_git_run()
        task = self.add_task(1, verify="python .assent/verify.py")
        self.start()
        adapter = ScriptedAdapter([])
        adapter.steps.append(self.isolated_done_step(
            adapter, task, {"src/ok.txt": "ok"}))

        self.assertEqual(self.run_engine(adapter, once=True), 0)
        self.assertEqual(parse_task_file(task).status, "DONE")
        self.assertIn(str((self.root / ".assent" / "verify.py").resolve()),
                      adapter.calls[0])

    def test_tracked_task_folder_is_rejected_before_worktree_creation(self):
        self.configure_git_run()
        task = self.add_task(1)
        self._git("add", "-f", str(task.relative_to(self.root)))
        self.start()
        worktree = gitops.worktree_path(self.root, "plan01")
        adapter = ScriptedAdapter([])

        self.assertEqual(self.run_engine(adapter), 1)
        self.assertEqual(adapter.calls, [])
        self.assertFalse(worktree.exists())

    def test_ignored_agents_md_is_passed_as_main_absolute_path(self):
        self.configure_git_run()
        task = self.add_task(1, verify="python .assent/verify.py")
        (self.root / ".gitignore").write_text(
            ".assent/\nAGENTS.md\n", encoding="utf-8")
        self.start()
        worktree = gitops.worktree_path(self.root, "plan01")
        adapter = ScriptedAdapter([])
        adapter.steps.append(self.isolated_done_step(adapter, task, {}))

        self.assertEqual(self.run_engine(adapter, once=True), 0)
        self.assertFalse((worktree / "AGENTS.md").exists())
        self.assertIn(str((self.root / "AGENTS.md").resolve()), adapter.calls[0])

    def test_missing_agents_md_does_not_block_execution(self):
        self.configure_git_run()
        task = self.add_task(1)
        (self.root / "AGENTS.md").unlink()
        self.start()
        worktree = gitops.worktree_path(self.root, "plan01")
        adapter = ScriptedAdapter([])
        adapter.steps.append(self.isolated_done_step(adapter, task, {}))

        self.assertEqual(self.run_engine(adapter, once=True), 0)
        self.assertTrue(worktree.exists())
        self.assertIn("skip if absent", adapter.calls[0])

    def test_two_folders_use_independent_worktrees_and_branches(self):
        self.configure_git_run()
        task1 = self.add_task(1)
        plan2 = self.root / ".assent" / "plan02"
        plan2.mkdir()
        task2 = plan2 / "t001_task.e.toml"
        task2.write_text(task_text(), encoding="utf-8", newline="\n")
        self.start()

        for folder, task, filename in (("plan01", task1, "one.py"),
                                       ("plan02", task2, "two.py")):
            adapter = ScriptedAdapter([])
            adapter.steps.append(self.isolated_done_step(
                adapter, task, {f"src/{filename}": folder}))
            cfg = load_config(self.root / ".assent" / "assent.toml", folder=folder)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 0)

        worktree1 = gitops.worktree_path(self.root, "plan01")
        worktree2 = gitops.worktree_path(self.root, "plan02")
        self.assertNotEqual(worktree1, worktree2)
        self.assertTrue((worktree1 / "src" / "one.py").is_file())
        self.assertFalse((worktree1 / "src" / "two.py").exists())
        self.assertTrue((worktree2 / "src" / "two.py").is_file())
        self.assertFalse((worktree2 / "src" / "one.py").exists())
        self.assertTrue(self.git_at(
            worktree1, "branch", "--show-current").strip().startswith("plan01/"))
        self.assertTrue(self.git_at(
            worktree2, "branch", "--show-current").strip().startswith("plan02/"))
        self.assertEqual(self._git("status", "--porcelain"), "")

    def test_busy_lock_refuses_before_creating_worktree(self):
        self.configure_git_run()
        self.add_task(1)
        self.start()
        cfg = self.cfg()
        worktree = gitops.worktree_path(self.root, "plan01")
        with hold_lock(cfg.tasks_dir, cfg.tasks_name):
            self.assertEqual(self.run_engine(ScriptedAdapter([])), 1)
        self.assertFalse(worktree.exists())


if __name__ == "__main__":
    unittest.main()
