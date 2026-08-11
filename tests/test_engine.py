"""engine tests: inject a ScriptedAdapter + fake sleep/now, and in a temporary repo verify
the core run path -- select task -> acceptance -> write-back -- plus focused verification,
prompt assembly and the scheduling refusals. The real CLI is never touched.

The session/quota/process rounds live in tests.test_engine_sessions, startup recovery and
main-tree escapes in tests.test_engine_recovery, and the read-only query and pre-session
decision tests in tests.test_inspection and tests.test_preflight. Shared fixtures come from
tests.engine_support.

Chinese literals that remain are deliberate user/upstream passthrough data (task titles,
notes, journal summaries, AGENTS.md content) used to prove that non-English data flows
through verbatim."""
import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from assent import auto_fix, engine, gitops, inspection
from assent.__main__ import _dispatch
from assent.adapters import TaskResult
from assent.config import load_config
from assent.lockfile import hold_lock
from assent.plan import (Plan, append_entry, journal_path_for, parse_task_file,
                         set_status)
from tests.engine_support import (_FAILV, _NEEDS_OK_TXT, _OK, EngineTestCase,
                                  ScriptedAdapter, ok_result, task_text)
from tests.link_support import make_directory_link, safe_rmtree
from tests.test_contracts import GlobalContractsMixin


class TestFocusedVerification(GlobalContractsMixin, EngineTestCase):
    def prepare_source(self):
        self.commit_all()
        worktree = gitops.ensure_worktree(self.root, "plan01")
        branch = gitops.ensure_branch(worktree, "plan01/")
        (worktree / "plan01.txt").write_text("source\n", encoding="utf-8")
        gitops.commit_all(worktree, "finish plan01")
        return worktree, gitops.branch_tip(self.root, branch)

    def test_focus_orders_done_tasks_deduplicates_runs_in_source_cwd(self):
        command = ('python -c "import pathlib,sys; '
                   "sys.exit(0 if pathlib.Path('plan01.txt').is_file() else 7)\"")
        self.write_task(1, slug="first", status="DONE", verify=command)
        self.write_task(2, slug="duplicate", status="DONE", verify=command)
        self.write_task(3, slug="skipped", status="SKIP",
                        verify=_FAILV)
        self.write_task(4, slug="unfinished", status="TODO", verify=_FAILV)
        cfg = self.build()
        worktree, source_tip = self.prepare_source()
        target_tip = gitops.commit_of(self.root, "HEAD")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = engine.verify_focused(cfg)

        self.assertEqual(result, 0, output.getvalue())
        text = output.getvalue()
        self.assertEqual(text.count(f"verify: {command}"), 1)
        self.assertIn("complete integration verification has not run", text)
        self.assertIn("cannot authorize `accept`", text)
        self.assertEqual(gitops.commit_of(self.root, "HEAD"), target_tip)
        self.assertEqual(
            gitops.branch_tip(self.root, gitops.current_branch(worktree)),
            source_tip)
        self.assertFalse((self.plan_dir / "_verification.toml").exists())
        self.assertFalse((self.root / ".assent" / "_batch_verification.toml").exists())
        self.assertTrue((worktree / "plan01.txt").is_file())

    def test_focus_stops_at_first_failed_done_task(self):
        later = ('python -c "import pathlib; '
                 "pathlib.Path('later.txt').write_text('ran')\"")
        self.write_task(1, slug="fails", status="DONE", verify=_FAILV)
        self.write_task(2, slug="later", status="DONE", verify=later)
        cfg = self.build()
        worktree, _source_tip = self.prepare_source()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = engine.verify_focused(cfg)

        self.assertEqual(result, 1)
        self.assertNotIn("verify: " + later, output.getvalue())
        self.assertFalse((worktree / "later.txt").exists())

    @contextlib.contextmanager
    def counted_verify(self, codes=None):
        """Script every focused command as a counted, subprocess-free result."""
        calls: list[str] = []

        def fake(_cfg, command):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, (codes or {}).get(command, 0), "", "")

        with mock.patch.object(engine, "_verify_subprocess", fake):
            yield calls

    def test_focus_runs_overlapping_unittest_modules_once_per_pass(self):
        first = "python -m unittest tests.alpha tests.shared"
        second = "python -m unittest tests.shared tests.beta"
        self.write_task(1, slug="first", status="DONE", verify=first)
        self.write_task(2, slug="second", status="DONE", verify=second)
        self.write_task(3, slug="other", status="DONE", verify=_OK)
        cfg = self.build()
        self.prepare_source()

        output = io.StringIO()
        with self.counted_verify() as calls, contextlib.redirect_stdout(output):
            result = engine.verify_focused(cfg)

        self.assertEqual(result, 0, output.getvalue())
        # tests.shared is imported once for the merged group; the command that
        # is not the exact unittest shape still runs byte-for-byte on its own.
        self.assertEqual(
            calls,
            ["python -m unittest tests.alpha tests.shared tests.beta", _OK])
        text = output.getvalue()
        for command in (first, second, _OK):
            self.assertEqual(text.count(f"  verify: {command}"), 1)
        self.assertEqual(text.count("verify passed (exit 0)"), 3)

    def test_focus_leaves_unmergeable_command_shapes_untouched(self):
        commands = ("python -m unittest tests.alpha", "pytest -q",
                    "python3 -m unittest tests.beta",
                    "python -m unittest -v tests.gamma",
                    "python -m unittest  tests.delta")
        for index, command in enumerate(commands, 1):
            self.write_task(index, slug=f"task{index}", status="DONE",
                            verify=command)
        cfg = self.build()
        self.prepare_source()

        output = io.StringIO()
        with self.counted_verify() as calls, contextlib.redirect_stdout(output):
            result = engine.verify_focused(cfg)

        self.assertEqual(result, 0, output.getvalue())
        # Only one command has the exact shape, so nothing is merged and every
        # invocation is identical to the unmerged behavior.
        self.assertEqual(calls, list(commands))

    def test_focus_falls_back_to_single_commands_when_the_merge_fails(self):
        first = "python -m unittest tests.alpha tests.shared"
        second = "python -m unittest tests.broken"
        merged = "python -m unittest tests.alpha tests.shared tests.broken"
        self.write_task(1, slug="first", status="DONE", verify=first)
        self.write_task(2, slug="second", status="DONE", verify=second)
        cfg = self.build()
        self.prepare_source()

        output = io.StringIO()
        codes = {merged: 1, second: 1}
        with self.counted_verify(codes) as calls, \
                contextlib.redirect_stdout(output):
            result = engine.verify_focused(cfg)

        self.assertEqual(result, 1)
        self.assertEqual(calls, [merged, first, second])
        text = output.getvalue()
        self.assertIn(f"  verify: {second}\n  verify failed (exit 1)", text)
        self.assertIn(f"  verify: {first}\n  verify passed (exit 0)", text)

    def test_focus_refuses_busy_folder_and_no_eligible_command(self):
        path = self.write_task(1, status="SKIP")
        self.write_task(2, slug="waiting", status="TODO")
        cfg = self.build()
        self.prepare_source()

        with hold_lock(cfg.tasks_dir, cfg.tasks_name):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = engine.verify_focused(cfg)
        self.assertEqual(result, 1)
        self.assertIn("refused", output.getvalue())

        # Release the lock, then prove the no-eligible-command refusal without
        # starting a subprocess or changing either Git identity.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = engine.verify_focused(cfg)
        self.assertEqual(result, 1)
        self.assertIn("no DONE task", output.getvalue())
        self.assertEqual(parse_task_file(path).status, "SKIP")


class TestRunSuccess(GlobalContractsMixin, EngineTestCase):
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

    def test_only_declared_base_must_be_ancestor_of_downstream(self):
        path = self.write_task(1)
        for folder in ("A", "B"):
            upstream = self.root / ".assent" / folder
            upstream.mkdir()
            (upstream / "t001_task.e.toml").write_text(
                task_text(status="DONE"), encoding="utf-8")
        (self.plan_dir / "_folder.toml").write_text(
            'after = ["A", "B"]\nbase = "A"\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        for folder in ("A", "B"):
            source = gitops.worktree_path(self.root, folder)
            self._git("worktree", "add", "-b", f"{folder}/run",
                      str(source), "HEAD")
            (source / f"{folder}.txt").write_text(
                f"{folder}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-A"], cwd=source,
                capture_output=True, encoding="utf-8", check=True)
            subprocess.run(
                ["git", "commit", "-m", f"finish {folder}"], cwd=source,
                capture_output=True, encoding="utf-8", check=True)
        adapter = ScriptedAdapter([self.ai_done(path, {"src/result.py": "ok"})])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = engine.run(cfg, once=True, adapter=adapter)

        self.assertEqual(result, 0, out.getvalue())
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertNotIn("stale stack", out.getvalue())

    def test_ordering_only_prerequisites_do_not_supply_a_stack_base(self):
        path = self.write_task(1)
        for folder in ("A", "B"):
            upstream = self.root / ".assent" / folder
            upstream.mkdir()
            (upstream / "t001_task.e.toml").write_text(
                task_text(status="DONE"), encoding="utf-8")
        (self.plan_dir / "_folder.toml").write_text(
            'after = ["A", "B"]\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        for folder in ("A", "B"):
            source = gitops.worktree_path(self.root, folder)
            self._git("worktree", "add", "-b", f"{folder}/run",
                      str(source), "HEAD")
            (source / f"{folder}.txt").write_text(
                f"{folder}\n", encoding="utf-8")
            subprocess.run(
                ["git", "add", "-A"], cwd=source,
                capture_output=True, encoding="utf-8", check=True)
            subprocess.run(
                ["git", "commit", "-m", f"finish {folder}"], cwd=source,
                capture_output=True, encoding="utf-8", check=True)
        adapter = ScriptedAdapter([self.ai_done(path, {"src/result.py": "ok"})])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = engine.run(cfg, once=True, adapter=adapter)

        self.assertEqual(result, 0, out.getvalue())
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertIn("Stacked upstream: none", out.getvalue())

    def test_archived_folder_prerequisite_creates_worktree_without_a_source(self):
        # Incident regression: the downstream's after named an upstream that had
        # already been accepted, cleaned and archived, so no base/* branch was
        # left to snapshot and worktree creation died on "has no base/* source
        # branch".  Roster membership alone must resolve it.
        path = self.write_task(1)
        (self.plan_dir / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")
        (self.root / ".assent" / "_archived.toml").write_text(
            "[[archived]]\n"
            'folder = "base"\n'
            'archived_at = "2026-01-01T00:00:00+00:00"\n',
            encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path, {"src/result.py": "ok"})])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = engine.run(cfg, once=True, adapter=adapter)

        self.assertEqual(result, 0, out.getvalue())
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue(gitops.worktree_path(self.root, "plan01").is_dir())
        # An archived upstream is already in the target, so it is no unaccepted
        # upstream and contributes no speculative base.
        self.assertIn("Stacked upstream: none", out.getvalue())

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
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
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
                inspection, "write_report",
                side_effect=PermissionError("report file locked")):
            self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertTrue(any(s.startswith("auto(plan01/t001): ")
                            for s in self.subjects()))

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
        p1 = self.write_task(1, effort="slight")
        cfg = self.build(extra_config=
            '[adapter.claude.default_effort]\nlite = "heavy"\n'
            '[adapter.claude.efforts]\nslight = "minimal"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][2], "minimal")

    def test_effort_default_applied_per_tier(self):
        p1 = self.write_task(1, model="lite")  # built-in lite default is normal
        cfg = self.build(extra_config=
            '[adapter.claude.efforts]\nnormal = "balanced"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        self.run_quiet(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0],
                         (adapter.calls[0][0], "lite", "balanced"))

    def test_empty_default_effort_still_sends_the_builtin_tier_effort(self):
        # An empty table no longer suppresses the built-in mapping: the lite tier keeps
        # its built-in "normal" and is translated to a concrete value, so the vendor CLI
        # default is never what decides the reasoning investment.
        p1 = self.write_task(1, model="lite")
        cfg = self.build(extra_config=
            '[adapter.claude.default_effort]\n'
            '[adapter.claude.efforts]\nnormal = "balanced"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)], resolved_model="sonnet")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run(cfg, once=True, adapter=adapter)
        self.assertEqual(adapter.calls[0][2], "balanced")
        self.assertIn("Session: claude | lite->sonnet | normal->balanced",
                      out.getvalue())
        self.assertNotIn("unspecified", out.getvalue())
        self.assertNotIn("CLI default", out.getvalue())

    def test_codex_uses_its_own_effort_translation(self):
        p1 = self.write_task(1, model="lite", effort="heavy")
        cfg = self.build(adapter_name="codex", extra_config=
            '[adapter.claude.efforts]\nheavy = "claude-value"\n'
            '[adapter.codex.efforts.lite]\nheavy = "max"\n')
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
        # The working instructions are the global contract; the task and journal
        # stay in the project's own management plane.
        self.assertIn(str(self.user_home / "instructions.md"), prompt)
        self.assertNotIn(str(cfg.assent_dir / "instructions.md"), prompt)
        self.assertIn(str(p1), prompt)
        self.assertIn(str(p1.with_name("t001_task.r.toml")), prompt)
        self.assertIn(_OK, prompt)
        self.assertIn('by = "claude"', prompt)
        self.assertIn('requested_model = "lite"', prompt)
        self.assertIn('abstract effort = "normal"', prompt)
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

    def test_session_line_states_the_four_facts_compactly(self):
        p1 = self.write_task(1, model="lite", effort="heavy")
        cfg = self.build(extra_config=
            '[adapter.claude.efforts.lite]\nheavy = "max"\n')
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run(cfg, once=True,
                       adapter=ScriptedAdapter([self.ai_done(p1)],
                                               resolved_model="sonnet"))
        lines = [line for line in out.getvalue().splitlines()
                 if "Session:" in line]
        self.assertEqual(lines, ["  Session: claude | lite->sonnet | heavy->max"])

    def test_worktree_verify_runs_with_the_worktree_as_cwd(self):
        # The main-tree expansion of `.assent/verify.py` was retired with the plan
        # parser's refusal of it as a task gate; a narrow gate resolves its own
        # relative paths, and what still has to hold is the cwd.
        cfg = self.build()
        worktree = self.root / "isolated"
        worktree.mkdir()
        (worktree / "probe.py").write_text(
            "from pathlib import Path\n"
            "Path('verified.txt').write_text('ok', encoding='utf-8')\n",
            encoding="utf-8")

        self.assertEqual(
            engine._run_verify(cfg.for_worktree(worktree), "python probe.py"), 0)
        self.assertEqual((worktree / "verified.txt").read_text(encoding="utf-8"),
                         "ok")
        self.assertFalse((self.root / "verified.txt").exists())


class TestAcceptanceGates(GlobalContractsMixin, EngineTestCase):
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

    def test_undeclared_link_at_closeout_is_a_normal_acceptance_failure(self):
        (self.root / ".gitignore").write_text(
            ".assent/\npkg/\n", encoding="utf-8")
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()
        external = self.root.parent / f"{self.root.name} external pkg"
        external.mkdir()
        self.addCleanup(safe_rmtree, external)
        (external / "sentinel.txt").write_text("keep\n", encoding="utf-8")

        def add_unreviewed_link(prompt):
            make_directory_link(self.execution_root() / "pkg", external)
            set_status(path, "DONE")
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="done",
                         summary="implemented")
            return ok_result()

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = engine.run(
                cfg, once=True,
                adapter=ScriptedAdapter([add_unreviewed_link]))

        self.assertEqual(result, 0, output.getvalue())
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertIn("Acceptance failed:", output.getvalue())
        self.assertIn("outside its active NO-IGNORED-DIRECTORY-CANDIDATE",
                      output.getvalue())
        self.assertNotIn("infrastructure error", output.getvalue())
        self.assertEqual(
            (external / "sentinel.txt").read_text(encoding="utf-8"), "keep\n")
        from assent.plan import read_entries
        scheduler_entry = next(
            entry for entry in read_entries(journal_path_for(path))
            if entry["by"] == "scheduler" and entry["event"] == "blocked")
        self.assertIn("outside its active NO-IGNORED-DIRECTORY-CANDIDATE",
                      scheduler_entry["summary"])

    def test_status_not_updated_but_verify_green_gets_closeout_suffix(self):
        path = self.write_task(1, verify=_OK)
        cfg = self.build(retry=1)
        self.commit_all()

        def leave_todo(prompt):
            return ok_result()

        def finish(prompt):
            self.assertIn(
                "This session must only close out the task", prompt)
            self.assertIn(_OK, prompt)
            self.assertNotIn(
                "review and fix it on top of what is there", prompt)
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([leave_todo, finish])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_status_not_updated_with_failing_verify_keeps_existing_prompt(self):
        path = self.write_task(1, verify=_FAILV)
        cfg = self.build(retry=1)
        self.commit_all()

        def leave_todo(prompt):
            return ok_result()

        def finish(prompt):
            self.assertIn(
                "Reason: Status not updated to DONE/BLOCKED (currently TODO).",
                prompt)
            self.assertIn(
                "review and fix it on top of what is there, do not redo it.",
                prompt)
            self.assertNotIn("only closeout missing", prompt)
            self.assertNotIn("focused verify already passed", prompt)
            set_status(path, "DONE")
            return ok_result()

        adapter = ScriptedAdapter([leave_todo, finish])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

    def test_closeout_only_blocked_record_notes_only_closeout_missing(self):
        path = self.write_task(1, verify=_OK)
        cfg = self.build(retry=0)
        self.commit_all()

        def implemented_but_forgot_status(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "impl.py").write_text("done", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([implemented_but_forgot_status])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertIn("src/impl.py", self._git_execution("ls-files"))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        blocked = next(e for e in entries
                       if e["by"] == "scheduler" and e["event"] == "blocked")
        self.assertIn("only closeout missing", blocked["summary"])
        self.assertTrue(any(
            "only closeout missing" in s for s in self.subjects()))

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

    def test_verify_echo_on_success(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=ScriptedAdapter([self.ai_done(path)])), 0)
        text = out.getvalue()
        self.assertIn(f"  verify: {_OK}", text)
        self.assertIn("  verify passed (exit 0)", text)
        self.assertLess(text.index(f"  verify: {_OK}"),
                        text.index("  verify passed (exit 0)"))

    def test_verify_echo_on_failure_keeps_tail_after_failed_line(self):
        failing_verify = ('python -c "import sys;'
                          "sys.stderr.write('boom'); sys.exit(3)\"")
        path = self.write_task(1, verify=failing_verify)
        cfg = self.build(retry=0)
        self.commit_all()

        def step(prompt):
            set_status(path, "DONE")
            return ok_result()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=ScriptedAdapter([step])), 0)
        text = out.getvalue()
        self.assertIn(f"  verify: {failing_verify}", text)
        self.assertIn("  verify failed (exit 3)", text)
        self.assertIn("boom", text)
        i_cmd = text.index(f"  verify: {failing_verify}")
        i_failed = text.index("  verify failed (exit 3)")
        i_tail = text.index("boom", i_failed)
        self.assertLess(i_cmd, i_failed)
        self.assertLess(i_failed, i_tail)


class TestSchedulingAndRefusals(GlobalContractsMixin, EngineTestCase):
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
                ("check", lambda: inspection.check(cfg))):
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
            self.assertEqual(inspection.check(cfg), 1)
        self.assertIn("retired legacy task files", check_out.getvalue())
        self.assertIn("move them", check_out.getvalue())

        run_out = io.StringIO()
        with contextlib.redirect_stdout(run_out):
            self.assertEqual(engine.run(cfg, adapter=adapter), 1)
        self.assertIn("retired legacy task files", run_out.getvalue())
        self.assertIn("move them", run_out.getvalue())
        self.assertEqual(adapter.calls, [])


class TestReworkPromptSuffix(GlobalContractsMixin, EngineTestCase):
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


class TestAutoFixFolderReviewGate(GlobalContractsMixin, EngineTestCase):
    def build_review(self, retry=1, rounds=1, model="core"):
        # The merged reviewer-fixer loop walks this list position by position,
        # so a case needing more than one reviewer session configures more than
        # one round.
        steps = ', { action = "focused_sweep" }, '.join(
            '{ role = "reviewer_fixer", adapter = "codex" }'
            for _ in range(rounds))
        return self.build(
            retry=retry,
            extra_config=(
                '[abilities.review_fix]\n'
                'prompt = "Review and repair within the named task scope."\n'
                'writes = true\n'
                'produces_verdict = true\n'
                '[abilities.fix]\n'
                'prompt = "Repair the durable findings."\n'
                'writes = true\n'
                '[roles.reviewer_fixer]\n'
                'ability = ["review_fix"]\n'
                f'model = "{model}"\n'
                'effort = "heavy"\n'
                '[roles.bounded_fixer]\n'
                'ability = ["fix"]\n'
                '[workflow]\n'
                'plan = [{ action = "focused_sweep" }, '
                f'{steps}, {{ action = "focused_sweep" }}]\n'))

    def write_pending_fail(self, cfg, verdict="FAIL"):
        task = parse_task_file(self.plan_dir / "t001_task.e.toml")
        rounds = cfg.workflow_plan
        self.assertTrue(rounds)
        review = next(
            step for step in rounds
            if getattr(step, "produces_verdict", False))
        # A PASS carries no findings; every other verdict must carry at least
        # one, so the fixture follows the verdict rather than the caller.
        findings = () if verdict == "PASS" else (auto_fix.ReviewFinding(
            task.id, "src/missing.py", "pending blocker",
            "restart evidence"),)
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord(verdict, findings),
            source_tree=gitops.tree_of(cfg.root, "HEAD"),
            task_plan_sha256=auto_fix.sha256_files((task.path,)),
            review_prompt_sha256="a" * 64,
            reviewer_role=review.role,
            reviewer_step_index=0,
            reviewer_adapter=review.adapter,
            reviewer_model=review.requested_model,
            reviewer_effort=review.requested_effort,
            workflow_step_index=1)
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        return state

    @contextlib.contextmanager
    def counted_verify(self, codes=None):
        """Script every focused command as a counted, subprocess-free result."""
        calls: list[str] = []

        def fake(_cfg, command):
            calls.append(command)
            return subprocess.CompletedProcess(
                command, (codes or {}).get(command, 0), "", "")

        with mock.patch.object(engine, "_verify_subprocess", fake):
            yield calls


    def test_shared_command_reuses_once_and_a_later_write_reruns_the_other(self):
        first = self.write_task(1, verify=_OK)
        second = self.write_task(2, slug="two", deps=("t001",), verify=_OK)
        third = self.write_task(3, slug="three", deps=("t002",),
                                verify=_NEEDS_OK_TXT)
        cfg = self.build_review()
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([TaskResult(0, terminal, False, None)])
        worker = ScriptedAdapter([
            self.ai_done(first, {"src/one.txt": "one\n"}),
            self.ai_done(second, {"src/two.txt": "two\n"}),
            self.ai_done(third, {"src/ok.txt": "ok\n"}),
        ])

        out = io.StringIO()
        with self.counted_verify() as calls, contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                ), 0)

        # Three closeout gates, then one final-gate execution: the shared command
        # last passed two checkpoints ago and is rerun exactly once for both of
        # its tasks, while t003's own unchanged pass is reused.
        self.assertEqual(calls, [_OK, _OK, _NEEDS_OK_TXT, _OK])
        self.assertEqual(out.getvalue().count("reused authoritative PASS"), 1)

    def test_reused_authoritative_pass_never_enters_the_merged_union(self):
        first = self.write_task(
            1, verify="python -m unittest tests.alpha tests.shared")
        second = self.write_task(
            2, slug="two", deps=("t001",),
            verify="python -m unittest tests.shared tests.beta")
        third = self.write_task(3, slug="three", deps=("t002",),
                                verify="python -m unittest tests.gamma")
        cfg = self.build_review()
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([TaskResult(0, terminal, False, None)])
        worker = ScriptedAdapter([
            self.ai_done(first, {"src/one.txt": "one\n"}),
            self.ai_done(second, {"src/two.txt": "two\n"}),
            self.ai_done(third, {"src/three.txt": "three\n"}),
        ])

        out = io.StringIO()
        with self.counted_verify() as calls, contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer,
                ), 0)

        # Three closeout gates, then one merged final run: t003's command is
        # still proven against this exact tree, so it is reused rather than
        # merged, and tests.gamma is never re-executed.
        self.assertEqual(calls, [
            "python -m unittest tests.alpha tests.shared",
            "python -m unittest tests.shared tests.beta",
            "python -m unittest tests.gamma",
            "python -m unittest tests.alpha tests.shared tests.beta",
        ])
        self.assertEqual(out.getvalue().count("reused authoritative PASS"), 1)

    def test_gate_evidence_binds_the_command_tree_and_clean_state(self):
        self.write_task(1, status="DONE", verify=_OK)
        cfg = self.build_review()
        self.commit_all()
        ledger = engine._FocusedGateLedger()

        ledger.record(cfg, _OK)
        self.assertTrue(ledger.reusable(cfg, _OK))
        self.assertFalse(ledger.reusable(cfg, _FAILV))

        source = self.root / "src" / "late.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("late\n", encoding="utf-8")
        self.assertFalse(ledger.reusable(cfg, _OK))
        # A pass proven while the worktree is dirty is not retained either.
        ledger.record(cfg, _FAILV)
        self.assertFalse(ledger.reusable(cfg, _FAILV))

        self.commit_all("late")
        self.assertFalse(ledger.reusable(cfg, _OK))
        self.assertFalse(engine._FocusedGateLedger().reusable(cfg, _OK))


    def test_unittest_shape_is_recognized_exactly_and_nothing_else(self):
        self.assertEqual(
            engine._unittest_modules("python -m unittest tests.a tests.b_2"),
            ("tests.a", "tests.b_2"))
        for command in ("python -m unittest",
                        "python -m unittest  tests.a",
                        "python -m unittest tests.a ",
                        "python3 -m unittest tests.a",
                        "python -m unittest -v tests.a",
                        "python -m unittest tests/a.py",
                        "python -m unittest tests.2bad",
                        "python -m pytest tests.a",
                        "pytest -q", _OK):
            self.assertIsNone(engine._unittest_modules(command), command)

    def test_final_sweep_runs_overlapping_unittest_modules_once(self):
        first = "python -m unittest tests.alpha tests.shared"
        second = "python -m unittest tests.shared tests.beta"
        merged = "python -m unittest tests.alpha tests.shared tests.beta"
        self.write_task(1, status="DONE", verify=first)
        self.write_task(2, slug="two", status="DONE", verify=second)
        self.write_task(3, slug="three", status="DONE", verify=_OK)
        cfg = self.build_review()
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def review(prompt):
            for command in (first, second, _OK):
                self.assertIn(f"- PASS: {command}", prompt)
            return TaskResult(0, terminal, False, None)

        reviewer = ScriptedAdapter([review])
        out = io.StringIO()
        with self.counted_verify() as calls, contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=reviewer), 0)

        # One merged unittest run proves both overlapping commands; the
        # non-unittest command still runs exactly as it did before.
        self.assertEqual(calls, [merged, _OK])
        text = out.getvalue()
        for command in (first, second, _OK):
            self.assertEqual(text.count(f"  verify: {command}"), 1)

    def test_failed_merge_falls_back_and_names_the_owning_command(self):
        first = "python -m unittest tests.alpha"
        second = "python -m unittest tests.broken"
        merged = "python -m unittest tests.alpha tests.broken"
        self.write_task(1, status="DONE", verify=first)
        self.write_task(2, slug="two", status="DONE", verify=second)
        cfg = self.build_review()
        self.commit_all()

        reviewer = ScriptedAdapter([])
        out = io.StringIO()
        codes = {merged: 1, second: 1}
        with self.counted_verify(codes) as calls, contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
                once=True), 1)

        self.assertEqual(calls, [merged, first, second])
        self.assertEqual(reviewer.calls, [])
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "FAIL")
        # Only the command that actually names the failing module is blamed.
        evidence = "\n".join(finding.evidence for finding in state.findings)
        self.assertIn(second, evidence)
        self.assertNotIn(first, evidence)
        self.assertEqual([finding.task_id for finding in state.findings],
                         ["t002"])



    def test_focused_failure_is_reviewed_then_repaired(self):
        task = self.write_task(1, status="DONE", verify=_NEEDS_OK_TXT)
        cfg = self.build_review()
        self.commit_all()

        def review(_prompt):
            pending = auto_fix.read_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg))
            finding = auto_fix.current_review_record(pending).findings[0]
            fingerprint = auto_fix.finding_fingerprint(finding)
            ready = self.execution_root() / "src" / "ok.txt"
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text("ready\n", encoding="utf-8")
            continued = auto_fix.ReviewFinding(
                finding.task_id, finding.path, finding.summary,
                finding.evidence, kind=finding.kind,
                recommendation=finding.recommendation,
                scope_addition=finding.scope_addition,
                transition="still_present", prior_fingerprint=fingerprint,
                transition_evidence=finding.evidence)
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("FIXED", (continued,))), False, None)

        reviewer = ScriptedAdapter([review])

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]),
            auto_fix_adapter=reviewer), 0)
        self.assertEqual(len(reviewer.calls), 1)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertTrue(any(
            finding.summary == "Final focused verification failed"
            for finding in state.findings))

    def test_failed_trailing_sweep_exhausts_roles_without_reusing_one(self):
        self.write_task(1, status="DONE", verify=_FAILV)
        cfg = self.build_review()
        self.commit_all()

        def review_and_try_fix(_prompt):
            pending = auto_fix.read_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg))
            finding = auto_fix.current_review_record(pending).findings[0]
            fingerprint = auto_fix.finding_fingerprint(finding)
            touched = self.execution_root() / "src" / "attempt.py"
            touched.parent.mkdir(parents=True, exist_ok=True)
            touched.write_text("attempted\n", encoding="utf-8")
            continued = auto_fix.ReviewFinding(
                finding.task_id, finding.path, finding.summary,
                finding.evidence, kind=finding.kind,
                recommendation=finding.recommendation,
                scope_addition=finding.scope_addition,
                transition="still_present", prior_fingerprint=fingerprint,
                transition_evidence="The configured role attempted the repair.")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("FIXED", (continued,))), False, None)

        reviewer = ScriptedAdapter([review_and_try_fix])
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertIsNotNone(state.unresolved_review)
        self.assertEqual(len(reviewer.calls), 1)


    def test_limited_focused_failure_supplies_exact_command_evidence(self):
        task = self.write_task(1, verify=_FAILV)
        cfg = self.build_review(retry=0)
        self.commit_all()
        finding = auto_fix.ReviewFinding(
            "t001", "src/result.py", "Focused gate remains red",
            "The scheduler supplied exit 3 for the task command.")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        reviewer = ScriptedAdapter([TaskResult(0, failed, False, None)])

        self.assertEqual(self.run_quiet(
            cfg, once=True,
            adapter=ScriptedAdapter([self.ai_done(task)]),
            auto_fix_adapter=reviewer), 1)

        prompt = reviewer.calls[0][0]
        self.assertIn("focused_gate_failure", prompt)
        self.assertIn(f"FAIL: exit 3: {_FAILV}", prompt)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.failure_trigger, "focused_gate_failure")
        self.assertEqual(parse_task_file(task).status, "BLOCKED")

    def test_blocked_adjudication_uses_trusted_contract_and_labels_tampering(self):
        task = self.write_task(1, scope=("src/",))
        cfg = self.build_review(retry=0)
        self.commit_all()
        finding = auto_fix.ReviewFinding(
            "t001", "src/owned.py", "Scope escape must be repaired",
            "The scheduler reported rogue.py outside the trusted src scope.")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))

        def tamper(_prompt):
            task.write_text(task_text(
                status="BLOCKED", scope=("src/", "rogue.py")),
                encoding="utf-8")
            (self.execution_root() / "rogue.py").write_text(
                "escaped\n", encoding="utf-8")
            return ok_result()

        reviewer = ScriptedAdapter([TaskResult(0, failed, False, None)])
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([tamper]),
            auto_fix_adapter=reviewer), 1)
        prompt = reviewer.calls[0][0]
        self.assertIn("task contract (trusted checkpoint; AUTHORITATIVE)", prompt)
        self.assertIn('scope = ["src/"]', prompt)
        self.assertIn("UNTRUSTED EVIDENCE", prompt)
        self.assertIn('scope = ["src/", "rogue.py"]', prompt)
        self.assertIn("Changes outside scope appeared: rogue.py", prompt)
        self.assertEqual(parse_task_file(task).scope, ["src/"])
        self.assertEqual(parse_task_file(task).status, "BLOCKED")

    def test_reviewer_pass_is_invalid_while_task_remains_blocked(self):
        task = self.write_task(1)
        cfg = self.build_review(retry=0)
        self.commit_all()

        def self_blocked(_prompt):
            set_status(task, "BLOCKED")
            return ok_result()

        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([TaskResult(0, passed, False, None)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=ScriptedAdapter([self_blocked]),
                auto_fix_adapter=reviewer), 1)
        self.assertIn("cannot PASS while a task remains BLOCKED", out.getvalue())
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())
        self.assertEqual(parse_task_file(task).status, "BLOCKED")

    def test_invalid_reviewer_output_restores_untrusted_task_structure(self):
        task = self.write_task(1, scope=("src/",))
        cfg = self.build_review(retry=0)
        self.commit_all()

        def tamper(_prompt):
            task.write_text(task_text(
                status="BLOCKED", scope=("src/", "rogue.py")),
                encoding="utf-8")
            return ok_result()

        reviewer = ScriptedAdapter([ok_result()])
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([tamper]),
            auto_fix_adapter=reviewer), 1)

        self.assertEqual(parse_task_file(task).scope, ["src/"])
        self.assertEqual(parse_task_file(task).status, "BLOCKED")
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())

    def test_independent_runnable_work_finishes_before_one_adjudication(self):
        first = self.write_task(1)
        second = self.write_task(2)
        cfg = self.build_review(retry=0)
        self.commit_all()

        def self_blocked(_prompt):
            set_status(first, "BLOCKED")
            return ok_result()

        invalid_scope = auto_fix.review_record_json(auto_fix.ReviewRecord(
            "FAIL", (auto_fix.ReviewFinding(
                "t001", "outside/task/scope.py", "Needs a human scope decision",
                "The scheduler cannot assign this proposed repair."),)))

        def review(prompt):
            self.assertEqual(parse_task_file(second).status, "DONE")
            self.assertEqual(prompt.count("Review context: BLOCKED_ADJUDICATION"), 1)
            return TaskResult(0, invalid_scope, False, None)

        worker = ScriptedAdapter([self_blocked, self.ai_done(second)])
        reviewer = ScriptedAdapter([review])
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer,
            ), 1)
        self.assertEqual(len(worker.calls), 2)
        self.assertEqual(len(reviewer.calls), 1)

    def test_full_run_recovers_prior_durable_blocker_from_journal(self):
        task = self.write_task(1, status="BLOCKED", verify=_FAILV)
        append_entry(
            journal_path_for(task), by="scheduler", event="blocked",
            summary=("Scheduler marked BLOCKED: Verify command exit code is "
                     f"non-zero (=3): {_FAILV}"),
            detail="Judged failed without any retry")
        cfg = self.build_review(retry=0)
        self.commit_all()
        invalid_scope = auto_fix.review_record_json(auto_fix.ReviewRecord(
            "FAIL", (auto_fix.ReviewFinding(
                "t001", "outside/task/scope.py", "Needs a human scope decision",
                "The durable focused failure was reproduced."),)))
        reviewer = ScriptedAdapter([
            TaskResult(0, invalid_scope, False, None)])

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
            ), 1)
        prompt = reviewer.calls[0][0]
        self.assertIn("[focused_gate_failure]", prompt)
        self.assertIn("Verify command exit code is non-zero", prompt)
        self.assertIn(_FAILV, prompt)

    def test_full_run_refuses_blocked_task_without_durable_evidence(self):
        self.write_task(1, status="BLOCKED")
        cfg = self.build_review(retry=0)
        self.commit_all()
        reviewer = ScriptedAdapter([])

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
            ), 1)
        self.assertEqual(reviewer.calls, [])

    def test_full_run_adjudicates_prior_and_new_blockers_together(self):
        first = self.write_task(1, status="BLOCKED")
        second = self.write_task(2)
        append_entry(
            journal_path_for(first), by="scheduler", event="blocked",
            summary="Scheduler marked BLOCKED: prior structural failure",
            detail="Judged failed without any retry")
        cfg = self.build_review(retry=0)
        self.commit_all()
        invalid_scope = auto_fix.review_record_json(auto_fix.ReviewRecord(
            "FAIL", (auto_fix.ReviewFinding(
                "t002", "outside/task/scope.py", "Needs a human scope decision",
                "The scheduler cannot assign this proposed repair."),)))

        def self_blocked(_prompt):
            set_status(second, "BLOCKED")
            append_entry(
                journal_path_for(second), by="claude", event="blocked",
                summary="New worker-authored blocker.")
            return ok_result()

        reviewer = ScriptedAdapter([
            TaskResult(0, invalid_scope, False, None)])
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([self_blocked]),
            auto_fix_adapter=reviewer), 1)

        prompt = reviewer.calls[0][0]
        self.assertIn("t001 [worker_blocked]: Scheduler marked BLOCKED: prior structural failure", prompt)
        self.assertIn("t002 [worker_blocked]: Execution AI self-marked BLOCKED", prompt)
        self.assertIn("worker journal summary (verbatim): New worker-authored blocker.", prompt)





    def test_pending_fail_refuses_recovery_after_builtin_identity_replaces_override(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        self.commit_all()
        before = self.write_pending_fail(cfg)
        drifted = self.build()
        worker = ScriptedAdapter([])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                drifted, adapter=worker), 1)

        self.assertEqual(worker.calls, [])
        self.assertIn("requires a configured plan review sequence", out.getvalue())
        self.assertEqual(
            auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(drifted)),
            before)

    def test_pending_fail_refuses_recovery_after_reviewer_identity_drift(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        self.commit_all()
        before = self.write_pending_fail(cfg)
        drifted = self.build_review(model="prime")
        worker = ScriptedAdapter([])
        reviewer = ScriptedAdapter([])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                drifted, adapter=worker, auto_fix_adapter=reviewer,
                ), 1)

        self.assertEqual(worker.calls, [])
        self.assertEqual(reviewer.calls, [])
        self.assertIn("reviewer identity no longer matches", out.getvalue())
        self.assertEqual(
            auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(drifted)),
            before)

    def test_pending_fail_cannot_close_an_ordinary_run_without_auto_fix(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        self.commit_all()
        before = self.write_pending_fail(cfg)
        ordinary = self.build()
        worker = ScriptedAdapter([])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(ordinary, adapter=worker), 1)

        self.assertEqual(worker.calls, [])
        self.assertIn("pending FAIL state", out.getvalue())
        self.assertEqual(
            auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(ordinary)),
            before)



    def test_every_review_prompt_combination_keeps_its_write_policy_coherent(self):
        """No context/stage/round cell may tell a reviewer two opposite things.

        The prompt is a product of review context, review stage and round
        position, but its parts were switched independently: the write policy
        branched on context while the round policy did not, so a read-only
        blocked adjudication was also handed the round sequence's final-round
        instruction to repair the blocker and return FIXED -- in the same
        prompt that forbids every write, and on the default single-adapter
        config. Point assertions cannot catch that; only the product can.
        """
        contexts = sorted(auto_fix.REVIEW_CONTEXTS)
        stages = sorted(auto_fix.REVIEW_STAGES)
        self.assertEqual(contexts, [
            "blocked_adjudication", "completed_folder",
            "selection_verification",
        ])
        self.assertEqual(stages, ["initial", "recheck"])

        self.write_task(1, status="DONE")
        total = 3
        cfg = self.build_review(rounds=total)
        self.commit_all()
        plan = Plan.parse(cfg.tasks_dir)
        # Text that only a round permitted to write may ever be given.
        repair_instructions = (
            "you may repair it directly",
            "This is the FINAL workflow plan step.",
            "and return FIXED",
        )

        for context in contexts:
            for stage in stages:
                for review_number in range(total):
                    with self.subTest(context=context, stage=stage,
                                      review_round=review_number + 1):
                        _tree, _digest, prompt, _prompt_digest = (
                            engine._auto_fix_review_identity(
                                cfg, plan, "focused evidence",
                                review_context=context, review_stage=stage,
                                round_index=review_number))
                        if context == "blocked_adjudication":
                            self.assertIn("never an\nimplementation session",
                                          prompt)
                            for phrase in repair_instructions:
                                self.assertNotIn(phrase, prompt)
                            # It is outside the sequence, so it must not claim
                            # a position in it either.
                            self.assertNotIn("This is review round", prompt)
                            continue
                        self.assertIn("you may repair it directly", prompt)
                        self.assertIn(
                            f"This is review round {review_number + 1} of {total}.",
                            prompt)
                        self.assertIn(
                            "Review rounds remaining after this one: "
                            f"{total - review_number - 1}.", prompt)
                        # Only the last position may claim finality, and it
                        # always must.
                        self.assertEqual(
                            "This is the FINAL review round." in prompt,
                            review_number == total - 1)



    def test_incomplete_limited_and_all_skip_folders_spend_no_review_tokens(self):
        first = self.write_task(1)
        second = self.write_task(2, deps=("t001",))
        cfg = self.build_review()
        self.commit_all()
        reviewer = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=ScriptedAdapter([self.ai_done(first)]),
                auto_fix_adapter=reviewer), 0)
        self.assertEqual(reviewer.calls, [])
        self.assertIn("review deferred after the limited run", out.getvalue())

        set_status(first, "SKIP")
        set_status(second, "SKIP")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=reviewer), 0)
        self.assertEqual(reviewer.calls, [])
        self.assertIn("all tasks are SKIP", out.getvalue())

    def test_run_without_workflow_plan_runs_normally(self):
        task = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            code = engine.run(
                cfg, adapter=ScriptedAdapter([self.ai_done(task)]))

        self.assertEqual(code, 0)
        self.assertEqual(parse_task_file(task).status, "DONE")
        self.assertNotIn("Auto-fix folder review", out.getvalue())


class TestReworkPromptFallbacks(GlobalContractsMixin, EngineTestCase):
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


class TestGlobalContractGate(GlobalContractsMixin, EngineTestCase):
    """No session may start against a missing or out-of-date global contract.

    The CLI refuses earlier with the same message, but these cases call
    ``engine.run`` directly, because the library entry point is the gate that
    actually has to hold.  Each one leaves a project ``.assent/instructions.md``
    in place: an old project copy is not a fallback, it is just a file.
    """

    def setUp(self):
        super().setUp()
        self.task = self.write_task(1)
        self.cfg = self.build()
        self.project_instructions = self.root / ".assent" / "instructions.md"
        self.project_instructions.write_text(
            "an older project's own working instructions\n", encoding="utf-8")
        self.commit_all()

    def refuse(self) -> str:
        """Run with no injected adapter, so resolving one at all is a failure."""
        out = io.StringIO()
        with mock.patch.object(
                engine, "get_adapter",
                side_effect=AssertionError(
                    "no adapter may be resolved once the contract gate fails")):
            with contextlib.redirect_stdout(out):
                self.assertEqual(engine.run(self.cfg, once=True), 1)
        text = out.getvalue()
        self.assertIn("Global contracts: FAIL", text)
        self.assertIn("assent init", text)
        # Nothing was started and nothing was written back.
        self.assertEqual(parse_task_file(self.task).status, "TODO")
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())
        return text

    def test_a_missing_contract_refuses_the_run(self):
        (self.user_home / "instructions.md").unlink()
        text = self.refuse()
        self.assertIn(str(self.user_home / "instructions.md"), text)
        self.assertIn("is missing", text)

    def test_a_stale_contract_refuses_the_run(self):
        (self.user_home / "format.md").write_text(
            "an older assent's plan format\n", encoding="utf-8")
        text = self.refuse()
        self.assertIn(str(self.user_home / "format.md"), text)
        self.assertIn("is stale", text)

    def test_the_refusal_never_falls_back_to_the_project_instructions(self):
        (self.user_home / "instructions.md").unlink()
        text = self.refuse()
        self.assertNotIn(str(self.project_instructions), text)


if __name__ == "__main__":
    unittest.main()
