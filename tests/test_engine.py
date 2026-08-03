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
import subprocess
import unittest
from unittest import mock

from assent import auto_fix, engine, gitops, inspection
from assent.adapters import TaskResult
from assent.config import load_config
from assent.lockfile import hold_lock
from assent.plan import append_entry, journal_path_for, parse_task_file, set_status
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
                         "claude|cli-model|normal|medium|t001")

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
    def build_review(self, retry=1):
        return self.build(
            retry=retry,
            extra_config=(
                '[auto_fix.review]\n'
                'adapter = "codex"\n'
                'model = "core"\n'
                'effort = "heavy"\n'))

    def write_pending_fail(self, cfg):
        task = parse_task_file(self.plan_dir / "t001_task.e.toml")
        review = cfg.auto_fix_review
        self.assertIsNotNone(review)
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (auto_fix.ReviewFinding(
                task.id, "src/missing.py", "pending blocker",
                "restart evidence"),)),
            source_tree=gitops.tree_of(cfg.root, "HEAD"),
            task_plan_sha256=auto_fix.sha256_files((task.path,)),
            review_prompt_sha256="a" * 64,
            reviewer_adapter=review.adapter,
            reviewer_model=review.requested_model,
            reviewer_effort=review.requested_effort)
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        return state

    def test_complete_folder_sweeps_distinct_checks_then_reuses_exact_pass(self):
        command = _OK
        self.write_task(1, status="DONE", verify=command)
        self.write_task(2, status="DONE", verify=command)
        cfg = self.build_review()
        self.commit_all()

        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def review(prompt):
            self.assertIn("Review context: COMPLETED_FOLDER", prompt)
            self.assertIn("Review stage: INITIAL", prompt)
            self.assertIn("cumulative checkpoint diff", prompt)
            self.assertIn("t001 task contract", prompt)
            self.assertIn(f"PASS: {command}", prompt)
            self.assertIn("directly necessary", prompt)
            self.assertIn("eligible technical debt", prompt)
            self.assertIn("task's declared scope", prompt)
            self.assertIn("repository-wide\ndebt search", prompt)
            self.assertIn("Complete verification has deliberately not run", prompt)
            return TaskResult(0, terminal, False, None)

        reviewer = ScriptedAdapter([review])
        reviewer.preflight = mock.Mock(return_value=[])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=reviewer, auto_fix=True), 0)
        self.assertEqual(len(reviewer.calls), 1)
        reviewer.preflight.assert_called_once()
        self.assertEqual(out.getvalue().count(f"  verify: {command}"), 1)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")

        cached = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=cached, auto_fix=True), 0)
        self.assertEqual(cached.calls, [])
        self.assertIn("reusing exact fresh PASS", out.getvalue())

    def test_focused_failure_starts_no_reviewer(self):
        task = self.write_task(1, status="DONE", verify=_NEEDS_OK_TXT)
        cfg = self.build_review()
        self.commit_all()
        reviewer = ScriptedAdapter([TaskResult(
            0, auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ())),
            False, None)])

        def repair(prompt):
            self.assertEqual(reviewer.calls, [])
            pending = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
            fingerprint = pending.current_finding_fingerprints[0]
            ready = self.execution_root() / "src" / "ok.txt"
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text("ready\n", encoding="utf-8")
            set_status(task, "DONE")
            append_entry(
                journal_path_for(task), by="claude", event="done",
                summary="Repaired the focused failure.",
                detail=(
                    'ASSENT_REPAIR_DISPOSITION {"fingerprint":"'
                    f'{fingerprint}","disposition":"fixed","detail":'
                    '"The focused command now passes."}'))
            return ok_result()

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([repair]),
            auto_fix_adapter=reviewer, auto_fix=True), 0)
        self.assertEqual(len(reviewer.calls), 1)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.phase, "COMPLETE")
        self.assertTrue(any(
            finding.summary == "Final focused verification failed"
            for finding in state.findings))

    def test_limited_self_blocked_attempt_is_adjudicated_without_a_fixer(self):
        task = self.write_task(1)
        cfg = self.build_review(retry=0)
        self.commit_all()
        finding = auto_fix.ReviewFinding(
            "t001", "src/blocker.py", "Worker blocker needs repair",
            "The task journal records the self-marked blocker.")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))

        def self_blocked(_prompt):
            set_status(task, "BLOCKED")
            append_entry(
                journal_path_for(task), by="claude", event="blocked",
                summary="Cannot satisfy the required invariant.")
            return ok_result()

        reviewer = ScriptedAdapter([TaskResult(0, failed, False, None)])
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([self_blocked]),
            auto_fix_adapter=reviewer, auto_fix=True), 1)

        self.assertEqual(len(reviewer.calls), 1)
        prompt = reviewer.calls[0][0]
        self.assertIn("Review context: BLOCKED_ADJUDICATION", prompt)
        self.assertIn("Review stage: INITIAL", prompt)
        self.assertIn("Execution AI self-marked BLOCKED", prompt)
        self.assertIn(
            "worker journal summary (verbatim): Cannot satisfy the required invariant.",
            prompt)
        self.assertIn("legitimately skips the focused gate", prompt)
        self.assertIn("integration candidate", prompt)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.phase, "NEEDS_REPAIR")
        self.assertEqual(state.failure_trigger, "worker_blocked")
        self.assertEqual(parse_task_file(task).status, "BLOCKED")

        fingerprint = auto_fix.finding_fingerprint(finding)

        def repair(_prompt):
            repaired = self.execution_root() / "src" / "blocker.py"
            repaired.parent.mkdir(parents=True, exist_ok=True)
            repaired.write_text("repaired\n", encoding="utf-8")
            set_status(task, "DONE")
            append_entry(
                journal_path_for(task), by="claude", event="done",
                summary="Repaired the blocker.",
                detail=(
                    'ASSENT_REPAIR_DISPOSITION {"fingerprint":"'
                    f'{fingerprint}","disposition":"fixed","detail":'
                    '"The required invariant now holds."}'))
            return ok_result()

        still_present = auto_fix.review_record_json(auto_fix.ReviewRecord(
            "FAIL", (auto_fix.ReviewFinding(
                finding.task_id, finding.path, finding.summary,
                finding.evidence, kind=finding.kind,
                recommendation=finding.recommendation,
                transition="still_present", prior_fingerprint=fingerprint,
                transition_evidence="The first repair still reproduces the blocker."),)))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        repairer = ScriptedAdapter([repair, repair])
        rechecker = ScriptedAdapter([
            TaskResult(0, still_present, False, None),
            TaskResult(0, passed, False, None),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=repairer, auto_fix_adapter=rechecker,
            auto_fix=True), 0)
        self.assertEqual(len(repairer.calls), 2)
        second_repair_prompt = repairer.calls[1][0]
        self.assertIn("Cannot satisfy the required invariant.",
                      second_repair_prompt)
        self.assertIn("legitimately skips the focused gate",
                      second_repair_prompt)
        self.assertNotIn("(none; this is a completed-folder review)",
                         second_repair_prompt)
        self.assertIn("Review context: BLOCKED_ADJUDICATION",
                      rechecker.calls[0][0])
        self.assertIn("Review stage: RECHECK", rechecker.calls[0][0])
        completed = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(completed.phase, "COMPLETE")
        self.assertEqual(parse_task_file(task).status, "DONE")

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
            auto_fix_adapter=reviewer, auto_fix=True), 1)

        prompt = reviewer.calls[0][0]
        self.assertIn("focused_gate_failure", prompt)
        self.assertIn(f"FAIL: exit 3: {_FAILV}", prompt)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.failure_trigger, "focused_gate_failure")
        self.assertEqual(parse_task_file(task).status, "BLOCKED")

    def test_blocked_recheck_keeps_the_original_focused_gate_trigger(self):
        task = self.write_task(1, verify=_NEEDS_OK_TXT)
        cfg = self.build_review(retry=0)
        self.commit_all()
        finding = auto_fix.ReviewFinding(
            "t001", "src/gate.py", "The focused gate must pass",
            "The scheduler supplied the failing focused command.")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        reviewer = ScriptedAdapter([TaskResult(0, failed, False, None)])
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([self.ai_done(task)]),
            auto_fix_adapter=reviewer, auto_fix=True), 1)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.failure_trigger, "focused_gate_failure")

        fingerprint = auto_fix.finding_fingerprint(finding)

        def repair(_prompt):
            ready = self.execution_root() / "src" / "ok.txt"
            ready.parent.mkdir(parents=True, exist_ok=True)
            ready.write_text("ready\n", encoding="utf-8")
            set_status(task, "DONE")
            append_entry(
                journal_path_for(task), by="claude", event="done",
                summary="Repaired the focused gate.",
                detail=(
                    'ASSENT_REPAIR_DISPOSITION {"fingerprint":"'
                    f'{fingerprint}","disposition":"fixed","detail":'
                    '"The focused command now passes."}'))
            return ok_result()

        # The repaired folder is complete, so the recheck collects no blocker at
        # all; the original focused-gate classification must survive both the
        # still-present round and the final PASS.
        still_present = auto_fix.review_record_json(auto_fix.ReviewRecord(
            "FAIL", (auto_fix.ReviewFinding(
                finding.task_id, finding.path, finding.summary,
                finding.evidence, kind=finding.kind,
                recommendation=finding.recommendation,
                transition="still_present", prior_fingerprint=fingerprint,
                transition_evidence="The repaired gate still fails locally."),)))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        rechecker = ScriptedAdapter([
            TaskResult(0, still_present, False, None),
            TaskResult(0, passed, False, None),
        ])
        seen: list[str] = []

        def repair_round(prompt):
            seen.append(auto_fix.read_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg)).failure_trigger)
            return repair(prompt)

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([repair_round, repair_round]),
            auto_fix_adapter=rechecker, auto_fix=True), 0)
        self.assertIn("Review stage: RECHECK", rechecker.calls[0][0])
        self.assertEqual(seen, ["focused_gate_failure", "focused_gate_failure"])
        completed = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(completed.review_context, "blocked_adjudication")
        self.assertEqual(completed.failure_trigger, "focused_gate_failure")
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn(
            "Original blocker: focused task gate failure durable evidence",
            report)

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
            auto_fix_adapter=reviewer, auto_fix=True), 1)
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
                auto_fix_adapter=reviewer, auto_fix=True), 1)
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
            auto_fix_adapter=reviewer, auto_fix=True), 1)

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
            auto_fix=True), 1)
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
            auto_fix=True), 1)
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
            auto_fix=True), 1)
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
            auto_fix_adapter=reviewer, auto_fix=True), 1)

        prompt = reviewer.calls[0][0]
        self.assertIn("t001 [worker_blocked]: Scheduler marked BLOCKED: prior structural failure", prompt)
        self.assertIn("t002 [worker_blocked]: Execution AI self-marked BLOCKED", prompt)
        self.assertIn("worker journal summary (verbatim): New worker-authored blocker.", prompt)

    def test_detectable_reviewer_write_is_preserved_and_cannot_pass(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def mutating_review(_prompt):
            (self.execution_root() / "reviewer-write.txt").write_text(
                "evidence\n", encoding="utf-8")
            return TaskResult(0, terminal, False, None)

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]),
            auto_fix_adapter=ScriptedAdapter([mutating_review]),
            auto_fix=True), 1)
        self.assertEqual(
            (self.execution_root() / "reviewer-write.txt").read_text(encoding="utf-8"),
            "evidence\n")
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())

    def test_runtime_log_and_unrelated_folder_progress_do_not_false_refuse(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        unrelated = cfg.assent_dir / "other"
        unrelated.mkdir()
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def concurrent_runtime_writes(_prompt):
            (cfg.tasks_dir / "_assent.log").write_text(
                "current run output\n", encoding="utf-8")
            (unrelated / "t001_task.r.toml").write_text(
                "parallel folder progress\n", encoding="utf-8")
            return TaskResult(0, terminal, False, None)

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]),
            auto_fix_adapter=ScriptedAdapter([concurrent_runtime_writes]),
            auto_fix=True), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")

    def test_current_folder_and_stable_root_management_writes_fail_closed(self):
        task = self.write_task(1, status="DONE")
        cfg = self.build_review()
        verifier = cfg.assent_dir / "verify.py"
        verifier.write_text("before\n", encoding="utf-8")
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def mutating_management(_prompt):
            journal_path_for(task).write_text(
                "reviewer interval evidence\n", encoding="utf-8")
            verifier.write_text("after\n", encoding="utf-8")
            return TaskResult(0, terminal, False, None)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=ScriptedAdapter([mutating_management]),
                auto_fix=True), 1)
        self.assertIn("writes were detected during the reviewer interval", out.getvalue())
        self.assertIn("management:verify.py", out.getvalue())
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())

    def test_root_security_state_writes_fail_closed(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))

        def mutating_management(_prompt):
            for name in ("manifest.toml", "_batch_verification.toml",
                         "_archived.toml"):
                (cfg.assent_dir / name).write_text(
                    "reviewer interval evidence\n", encoding="utf-8")
            archive = cfg.assent_dir / "_archive"
            archive.mkdir()
            (archive / "plan00.zip").write_bytes(b"reviewer archive mutation")
            return TaskResult(0, terminal, False, None)

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=ScriptedAdapter([mutating_management]),
                auto_fix=True), 1)
        for name in ("manifest.toml", "_batch_verification.toml",
                     "_archived.toml"):
            self.assertIn(f"management:{name}", out.getvalue())
        self.assertIn("management:_archive", out.getvalue())
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())

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
                drifted, adapter=worker, auto_fix=True), 1)

        self.assertEqual(worker.calls, [])
        self.assertIn("reviewer identity no longer matches", out.getvalue())
        self.assertEqual(
            auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(drifted)),
            before)

    def test_pending_fail_refuses_recovery_after_reviewer_identity_drift(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review()
        self.commit_all()
        before = self.write_pending_fail(cfg)
        drifted = self.build(extra_config=(
            '[auto_fix.review]\n'
            'adapter = "codex"\n'
            'model = "prime"\n'
            'effort = "heavy"\n'))
        worker = ScriptedAdapter([])
        reviewer = ScriptedAdapter([])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                drifted, adapter=worker, auto_fix_adapter=reviewer,
                auto_fix=True), 1)

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

    def test_invalid_response_retries_then_persists_valid_fail(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review(retry=1)
        self.commit_all()
        finding = auto_fix.ReviewFinding(
            "t001", "docs/missing.md", "Required test is missing",
            "The acceptance case has no regression test.")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        reviewer = ScriptedAdapter([
            TaskResult(0, "not a review record", False, None),
            TaskResult(0, failed, False, None),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]),
            auto_fix_adapter=reviewer, auto_fix=True), 1)
        self.assertEqual(len(reviewer.calls), 2)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "FAIL")
        self.assertEqual(state.findings[0].evidence, finding.evidence)

    def test_checkpoint_resume_and_quota_continue_without_consuming_invalid_retry(self):
        self.write_task(1, status="DONE")
        cfg = self.build_review(retry=0)
        self.commit_all()
        terminal = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([
            TaskResult(1, '{"type":"assent.checkpoint_resume"}', False,
                       None, checkpoint_resume=True),
            TaskResult(1, "quota", True, None),
            TaskResult(0, terminal, False, None),
        ])
        sleeps = []
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]), auto_fix_adapter=reviewer,
            auto_fix=True, sleep=sleeps.append), 0)
        self.assertEqual(len(reviewer.calls), 3)
        self.assertEqual(sum(sleeps), cfg.quota_poll_minutes * 60)
        self.assertLessEqual(max(sleeps), 60)

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
                auto_fix_adapter=reviewer, auto_fix=True), 0)
        self.assertEqual(reviewer.calls, [])
        self.assertIn("review deferred after the limited run", out.getvalue())

        set_status(first, "SKIP")
        set_status(second, "SKIP")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=reviewer, auto_fix=True), 0)
        self.assertEqual(reviewer.calls, [])
        self.assertIn("all tasks are SKIP", out.getvalue())

    def test_configured_review_without_auto_fix_flag_is_inert(self):
        self.write_task(1, status="DONE", verify=_FAILV)
        cfg = self.build_review()
        self.commit_all()
        reviewer = ScriptedAdapter([])
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=reviewer), 0)

        self.assertEqual(reviewer.calls, [])
        self.assertNotIn("Auto-fix folder review", out.getvalue())
        self.assertNotIn(f"verify: {_FAILV}", out.getvalue())
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())


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
