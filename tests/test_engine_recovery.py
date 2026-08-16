"""engine tests for recovering work the scheduler never got to record cleanly.

Two situations, both of which must keep every produced byte: a worktree left dirty by an
unclean process exit (hard power loss / kill) that never reached an interrupt handler, and a
session that wrote into the main tree instead of its isolated worktree. Shared fixtures come
from tests.engine_support, plus GlobalContractsMixin for the temporary user home that `run`
now requires.

Chinese literals that remain are deliberate user/upstream passthrough data."""
import contextlib
import io
import json
import re
import subprocess
import unittest
from unittest import mock

from assent import auto_fix, engine, gitops
from assent.adapters import TaskResult
from assent.plan import (Plan, append_entry, journal_path_for, parse_task_file,
                         read_entries, set_status)
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result
from tests.test_contracts import GlobalContractsMixin

# Each writable verdict role owns the bounded review-and-repair round between
# two scheduler-owned focused sweeps.
TWO_REVIEW_ROUNDS = '''
[abilities.review_fix]
prompt = "Review and repair."
writes = true
produces_verdict = true
[abilities.fix]
prompt = "Repair durable findings."
writes = true
[roles.folder_reviewer]
ability = ["review_fix"]
model = "prime"
effort = "heavy"
[roles.bounded_fixer]
ability = ["fix"]
[workflow]
plan = [
  { action = "focused_sweep" },
  { role = "folder_reviewer", adapter = "claude" },
  { action = "focused_sweep" },
  { role = "folder_reviewer", adapter = "claude" },
  { action = "focused_sweep" },
]
'''


class TestAutoFixRestartRecovery(GlobalContractsMixin, EngineTestCase):
    def repair_done(self, task_path, files=None, *, requested_model="lite"):
        def step(prompt):
            for rel, content in (files or {}).items():
                path = self.execution_root() / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            fingerprints = tuple(dict.fromkeys(re.findall(
                r"(?m)^- fingerprint: ([0-9a-f]{64})$", prompt)))
            detail = "\n".join(
                "ASSENT_REPAIR_DISPOSITION " + json.dumps({
                    "fingerprint": fingerprint,
                    "disposition": "fixed",
                    "detail": "The recovered repair now passes its focused gate.",
                }, separators=(",", ":"), sort_keys=True)
                for fingerprint in fingerprints)
            set_status(task_path, "DONE")
            append_entry(
                journal_path_for(task_path), by="claude",
                requested_model=requested_model, event="done",
                summary="Recovered repair completed", detail=detail)
            return ok_result()
        return step

    def test_pending_selection_repair_defers_ordinary_folder_execution(self):
        task_path = self.write_task(1, status="TODO", scope=("src/",))
        cfg = self.build()
        self.commit_all()
        plan = Plan.parse(cfg.tasks_dir)
        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "Selection repair is pending",
            "The durable complete-verifier evidence owns this repair.")
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (finding,)),
            source_tree=gitops.tree_of(cfg.root, "HEAD"),
            task_plan_sha256=engine._contracts_digest(
                plan, engine._task_contract_snapshots(plan)),
            review_prompt_sha256="2" * 64,
            reviewer_role="selection_reviewer",
            reviewer_adapter="claude", reviewer_model="prime",
            reviewer_effort="high",
            review_context="selection_verification")
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)
        adapter = ScriptedAdapter([])

        self.assertEqual(self.run_quiet(
            cfg, adapter=adapter), 0)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(task_path).status, "TODO")


class TestCrashDirtyWorktreeRecovery(GlobalContractsMixin, EngineTestCase):
    """Startup recovery of a worktree left dirty by work the scheduler never recorded: an
    unclean process exit (hard power loss / kill) that never reached the Ctrl+C / quota /
    infrastructure interrupt handlers, and -- from the merged reviewer-fixer round, which may
    write source itself -- a round interrupted after it wrote and before its verdict was
    recorded."""

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

    def _commit_in_worktree(self, worktree, subject):
        """Commit whatever is currently in the worktree under an exact checkpoint subject."""
        for args in (("add", "-A"), ("commit", "-m", subject)):
            subprocess.run(["git", *args], cwd=worktree, capture_output=True,
                           encoding="utf-8", check=True)

    def test_uncheckpointed_done_work_recovers_against_its_own_task(self):
        """The observed crash: the AI marked t001 DONE and the scheduler died before writing
        its auto checkpoint, so next_task() already points at the disjoint-scope t002."""
        first = self.write_task(1, status="DONE", scope=("src/",))
        second = self.write_task(2, slug="next", scope=("other/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "done_work.py").write_text("produced", encoding="utf-8")

        adapter = ScriptedAdapter([self.ai_done(first)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertTrue(
            any(s.startswith("wip(plan01/t001): recovered dirty worktree")
                for s in self.subjects()))
        # t001 is resumed as its own task; t002 never sees the work or a session.
        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(first).status, "DONE")
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertRegex(report, r"t001  DONE\s+.*\[[0-9a-f]+\]")
        self.assertEqual(parse_task_file(second).status, "TODO")
        self.assertFalse(any(s.startswith("wip(plan01/t002)") for s in self.subjects()))
        self.assertEqual(
            (worktree / "src" / "done_work.py").read_text(encoding="utf-8"), "produced")

    def test_two_plausible_uncheckpointed_done_tasks_stay_fail_closed(self):
        first = self.write_task(1, status="DONE", scope=("src/",))
        second = self.write_task(2, slug="also", status="DONE", scope=("src/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "ambiguous.py").write_text("x", encoding="utf-8")

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 1)
        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(first).status, "DONE")
        self.assertEqual(parse_task_file(second).status, "DONE")
        self.assertFalse(any(s.startswith("wip(plan01/") for s in self.subjects()))

    def test_already_checkpointed_done_task_is_not_reopened(self):
        """A terminal auto() checkpoint is proof the scheduler already closed that task out, so
        the remaining dirt is someone else's and no resumable candidate owns it."""
        path = self.write_task(1, status="DONE", scope=("src/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "checkpointed.py").write_text("done", encoding="utf-8")
        self._commit_in_worktree(worktree, "auto(plan01/t001): 任務")
        (worktree / "src" / "stray.py").write_text("x", encoding="utf-8")

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 1)
        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(any(s.startswith("wip(plan01/") for s in self.subjects()))

    def test_prefixed_checkpoint_marker_refuses_dirty_task_ownership(self):
        path = self.write_task(1, status="DONE", scope=("src/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "checkpointed.py").write_text(
            "done", encoding="utf-8")
        self._commit_in_worktree(
            worktree, "[HOOK] auto(plan01/t001): task complete")
        (worktree / "src" / "stray.py").write_text("x", encoding="utf-8")

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 1)

        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(any(s.startswith("wip(plan01/") for s in self.subjects()))

    def test_dirt_outside_the_done_task_scope_stays_fail_closed(self):
        path = self.write_task(1, status="DONE", scope=("src/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "inside.py").write_text("x", encoding="utf-8")
        (worktree / "outside.txt").write_text("stray", encoding="utf-8")

        adapter = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True, adapter=adapter), 1)
        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(adapter.calls, [])
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(any(s.startswith("wip(plan01/") for s in self.subjects()))

    def test_wip_backed_done_task_without_auto_commit_is_not_reopened(self):
        """A clean legacy DONE task backed only by WIP remains reviewable without history rewrite."""
        first = self.write_task(1, status="DONE", scope=("src/",))
        second = self.write_task(2, slug="next", scope=("other/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "stored.py").write_text("stored", encoding="utf-8")
        self._commit_in_worktree(worktree, "wip(plan01/t001): quota interrupt, progress kept")

        adapter = ScriptedAdapter([self.ai_done(second)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(first).status, "DONE")
        self.assertEqual(parse_task_file(second).status, "DONE")
        self.assertEqual(len(adapter.calls), 1)
        self.assertNotIn("resume", adapter.calls[0][0])
        self.assertFalse(any(s.startswith("wip(plan01/t001): recovered")
                             for s in self.subjects()))
        self.assertFalse(any(s.startswith("auto(plan01/t001): ")
                             for s in self.subjects()))

    def test_wip_resume_creates_empty_terminal_auto_and_report_attribution(self):
        path = self.write_task(1, status="WIP", title="Resume stored work",
                               scope=("src/",))
        cfg = self.build()
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "stored.py").write_text("stored", encoding="utf-8")
        self._commit_in_worktree(worktree, "wip(plan01/t001): progress kept")

        self.assertEqual(
            self.run_quiet(cfg, once=True,
                           adapter=ScriptedAdapter([self.ai_done(path)])), 0)

        subjects = self.subjects()
        autos = [s for s in subjects if s.startswith("auto(plan01/t001): ")]
        self.assertEqual(autos, ["auto(plan01/t001): Resume stored work"])
        auto_hash = self._git_execution("rev-parse", "HEAD").strip()
        self.assertEqual(
            self._git_execution("diff-tree", "--no-commit-id", "--name-only",
                                "-r", "HEAD").strip(), "")
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        self.assertIn(f"t001  DONE     Resume stored work  [{auto_hash[:7]}]", report)

    # An interrupted merged reviewer-fixer round: the round wrote source, was
    # interrupted before its verdict, and deliberately left the durable state
    # untouched so its position does not advance.  The task it was repairing is
    # DONE with its terminal auto() checkpoint, so neither owner above can claim
    # the remaining dirt -- only the folder's durable in-flight state can.

    def _reviewed_folder(self, extra_tasks=(), *, scope=("src/",)):
        """A folder whose tasks all finished their ordinary closeout: DONE, each with its own
        terminal auto() checkpoint, which is exactly what the two existing owners cannot
        claim dirt against."""
        path = self.write_task(1, status="DONE", scope=scope)
        for num, slug in extra_tasks:
            self.write_task(num, slug=slug, status="DONE", scope=scope)
        cfg = self.build(extra_config=TWO_REVIEW_ROUNDS)
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "value.txt").write_text("closed out\n", encoding="utf-8")
        self._commit_in_worktree(worktree, "auto(plan01/t001): 任務")
        for num, _slug in extra_tasks:
            (worktree / "src" / f"value{num}.txt").write_text(
                "closed out\n", encoding="utf-8")
            self._commit_in_worktree(worktree, f"auto(plan01/t{num:03d}): 任務")
        return cfg, path, worktree

    def _write_review_state(self, cfg, *, phase="AWAITING_REVIEW",
                            task_ids=("t001",)):
        """The durable record a review round in flight left behind, exactly as the loop writes
        it: the round that decided FAIL, its findings, and the phase it was interrupted in."""
        plan = Plan.parse(cfg.tasks_dir)
        review = cfg.workflow_plan[3]
        findings = tuple(
            auto_fix.ReviewFinding(task_id, "src/value.txt", "stale value",
                                   "review evidence")
            for task_id in task_ids)
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", findings),
            source_tree=gitops.tree_of(self.execution_root(), "HEAD"),
            task_plan_sha256=auto_fix.sha256_files(
                task.path for task in plan.tasks),
            review_prompt_sha256="3" * 64,
            reviewer_role=review.role,
            reviewer_step_index=1,
            reviewer_adapter=review.adapter,
            reviewer_model=review.requested_model,
            reviewer_effort=review.requested_effort,
            workflow_step_index=2)
        if state.phase != phase:
            state = auto_fix.with_repair_phase(state, phase)
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)

    def _assert_refused(self, cfg, worktree):
        """The dirt reaches ensure_clean: no session, no checkpoint, nothing committed."""
        worker = ScriptedAdapter([])
        reviewer = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer), 1)
        self.assertIn("Working tree is not clean", out.getvalue())
        self.assertEqual(worker.calls, [])
        self.assertEqual(reviewer.calls, [])
        self.assertFalse(any(s.startswith("wip(plan01/") for s in self.subjects()))
        self.assertFalse(gitops.working_tree_status(
            worktree, cfg.git_excludes).is_clean)

    def test_same_dirt_without_durable_state_stays_fail_closed(self):
        cfg, path, worktree = self._reviewed_folder()
        (worktree / "src" / "value.txt").write_text(
            "interrupted reviewer repair\n", encoding="utf-8")

        self._assert_refused(cfg, worktree)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(journal_path_for(path).exists())

    def test_writable_plan_review_boundary_recovers_and_direct_run_continues(self):
        path = self.write_task(1, status="DONE", scope=("src/",))
        cfg = self.build(extra_config='''
[abilities.review_fix]
prompt = "Review and repair."
writes = true
produces_verdict = true
[roles.folder_reviewer]
ability = ["review_fix"]
model = "prime"
effort = "heavy"
[workflow]
plan = [
  { role = "folder_reviewer", adapter = "claude" },
  { action = "focused_sweep" },
]
''')
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "value.txt").write_text(
            "closed out\n", encoding="utf-8")
        self._commit_in_worktree(worktree, "auto(plan01/t001): task complete")

        def interrupted_review(_prompt):
            (worktree / "src" / "value.txt").write_text(
                "reviewer repair\n", encoding="utf-8")
            raise KeyboardInterrupt

        self.assertEqual(
            self.run_quiet(
                cfg, auto_fix_adapter=ScriptedAdapter([interrupted_review])),
            130)
        self.assertTrue(auto_fix.auto_fix_review_session_path(cfg).is_file())
        self.assertFalse(gitops.working_tree_status(
            worktree, cfg.git_excludes).is_clean)

        passed_review = TaskResult(
            0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)
        self.assertEqual(
            self.run_quiet(
                cfg, auto_fix_adapter=ScriptedAdapter([passed_review])),
            0)

        self.assertTrue(gitops.working_tree_status(
            worktree, cfg.git_excludes).is_clean)
        self.assertFalse(auto_fix.auto_fix_review_session_path(cfg).exists())
        self.assertTrue(any(
            subject.startswith(
                "wip(plan01): recovered interrupted plan-review output")
            for subject in self.subjects()))
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(
            (worktree / "src" / "value.txt").read_text(encoding="utf-8"),
            "reviewer repair\n")

    def test_dirt_outside_the_implicated_task_scope_stays_fail_closed(self):
        cfg, path, worktree = self._reviewed_folder()
        self._write_review_state(cfg)
        (worktree / "src" / "value.txt").write_text(
            "interrupted reviewer repair\n", encoding="utf-8")
        (worktree / "outside.txt").write_text("stray", encoding="utf-8")

        self._assert_refused(cfg, worktree)
        self.assertEqual(
            (worktree / "outside.txt").read_text(encoding="utf-8"), "stray")

    def test_phase_that_is_not_in_flight_does_not_activate_the_new_owner(self):
        # NEEDS_REPAIR is the verdict written before any repair session runs, so
        # no round was writing and the dirt has no proven origin.
        cfg, path, worktree = self._reviewed_folder()
        self._write_review_state(cfg, phase="NEEDS_REPAIR")
        (worktree / "src" / "value.txt").write_text(
            "unexplained\n", encoding="utf-8")

        self._assert_refused(cfg, worktree)

    def test_two_implicated_tasks_that_could_own_the_dirt_stay_fail_closed(self):
        cfg, path, worktree = self._reviewed_folder(extra_tasks=((2, "also"),))
        self._write_review_state(cfg, task_ids=("t001", "t002"))
        (worktree / "src" / "value.txt").write_text(
            "ambiguous\n", encoding="utf-8")

        self._assert_refused(cfg, worktree)

    def test_resumed_task_that_fails_a_gate_gets_no_empty_terminal_auto(self):
        path = self.write_task(
            1, status="WIP", scope=("src/",),
            verify='python -c "raise SystemExit(3)"')
        cfg = self.build(retry=0)
        self.commit_all()
        worktree = self._reused_worktree()
        (worktree / "src").mkdir()
        (worktree / "src" / "stored.py").write_text("stored", encoding="utf-8")
        self._commit_in_worktree(worktree, "wip(plan01/t001): progress kept")

        self.assertEqual(
            self.run_quiet(cfg, once=True,
                           adapter=ScriptedAdapter([self.ai_done(path)])), 0)

        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertFalse(any(s.startswith("auto(plan01/t001): ")
                             for s in self.subjects()))


class TestMainTreeEscapeDetection(GlobalContractsMixin, EngineTestCase):
    """A session is expected to write only into its isolated worktree (self.execution_root());
    these tests reproduce a session instead writing into the main tree (self.root) and check
    the scheduler's mechanical detect + port-back-or-fail-closed handling."""

    def test_no_new_main_tree_dirt_is_byte_identical_to_today(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        # Dirt already present in the main tree before the session starts (e.g. left over
        # from unrelated manual work) is the pre-session baseline, not an escape.
        (self.root / "leftover.txt").write_text("leftover\n", encoding="utf-8")

        adapter = ScriptedAdapter([self.ai_done(path, {"src/a.py": "ok"})])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(
            (self.root / "leftover.txt").read_text(encoding="utf-8"), "leftover\n")
        from assent.plan import read_entries
        self.assertFalse(any(e.get("event") == "main_tree_escape"
                             for e in read_entries(journal_path_for(path))))
        self.assertTrue(any(s.startswith("auto(plan01/t001): ")
                            for s in self.subjects()))

    def test_in_scope_escape_is_ported_back_and_main_tree_restored(self):
        # Exact scopes below a wholly-new directory exercise detailed untracked
        # path discovery in both the main-tree escape and task closeout gates.
        path = self.write_task(
            1, scope=("src/leaked.py", "src/formal.py"))
        cfg = self.build()         # default retry=1: one retry survives the escaped attempt
        self.commit_all()

        def leak_step(prompt):
            leaked = self.root / "src" / "leaked.py"
            leaked.parent.mkdir(parents=True, exist_ok=True)
            leaked.write_text("leaked-content\n", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter(
            [leak_step, self.ai_done(path, {"src/formal.py": "ok"})])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertEqual(len(adapter.calls), 2)
        # The main tree is restored: the escaped path no longer exists there.
        self.assertFalse((self.root / "src" / "leaked.py").exists())
        # The escaped content survived, ported into the worktree (never discarded), and the
        # next attempt's own output is there too.
        self.assertEqual(
            (self.execution_root() / "src" / "leaked.py").read_text(encoding="utf-8"),
            "leaked-content\n")
        self.assertEqual(
            (self.execution_root() / "src" / "formal.py").read_text(encoding="utf-8"), "ok")

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        escapes = [e for e in entries if e.get("event") == "main_tree_escape"]
        self.assertEqual(len(escapes), 1)
        self.assertEqual(escapes[0]["by"], "scheduler")
        self.assertIn("src/leaked.py", escapes[0]["detail"])

    def test_escape_apply_failure_leaves_both_trees_untouched(self):
        path = self.write_task(1, scope=("src/",))
        src_dir = self.root / "src"
        src_dir.mkdir()
        (src_dir / "original.py").write_text("a\nb\nc\n", encoding="utf-8")
        cfg = self.build(retry=0)
        self.commit_all()

        def diverge_step(prompt):
            # A normal, legitimate worktree edit ...
            (self.execution_root() / "src" / "original.py").write_text(
                "x\nb\nc\n", encoding="utf-8")
            # ... and, separately, an overlapping edit that escaped into the main tree; the
            # escaped diff's context no longer matches the worktree's own edit, so the patch
            # cannot apply cleanly.
            (self.root / "src" / "original.py").write_text(
                "a\ny\nc\n", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([diverge_step])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        # Both trees are left exactly as the step wrote them: nothing ported, nothing
        # reverted, nothing discarded.
        self.assertEqual(
            (self.root / "src" / "original.py").read_text(encoding="utf-8"), "a\ny\nc\n")
        self.assertEqual(
            (self.execution_root() / "src" / "original.py").read_text(encoding="utf-8"),
            "x\nb\nc\n")

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(e.get("event") == "main_tree_escape" for e in entries))
        blocked = next(e for e in entries if e.get("event") == "blocked")
        self.assertIn("port back manually", blocked["summary"])

    def test_out_of_scope_escape_leaves_main_tree_untouched(self):
        path = self.write_task(1, scope=("src/",))
        cfg = self.build(retry=0)
        self.commit_all()

        def leak_outside_step(prompt):
            (self.root / "outside.txt").write_text("stray\n", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([leak_outside_step])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertEqual(
            (self.root / "outside.txt").read_text(encoding="utf-8"), "stray\n")
        self.assertFalse((self.execution_root() / "outside.txt").exists())

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(e.get("event") == "main_tree_escape" for e in entries))
        blocked = next(e for e in entries if e.get("event") == "blocked")
        self.assertIn("outside.txt", blocked["summary"])
        self.assertIn("outside this task's scope", blocked["summary"])


if __name__ == "__main__":
    unittest.main()
