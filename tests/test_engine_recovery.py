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

# The merged reviewer-fixer loop is finite because it walks the configured
# [auto_fix.review] list position by position, so a case that needs a first
# round and a later confirming round must configure exactly two rounds.
TWO_REVIEW_ROUNDS = '\n[auto_fix.review]\nadapter = ["claude", "claude"]\n'


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

    def test_interrupt_preserves_edits_and_restart_reuses_the_same_profile(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=TWO_REVIEW_ROUNDS)

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "stale value", "review evidence")
        failed = auto_fix.review_record_json(
            auto_fix.ReviewRecord("FAIL", (finding,)))
        passed = auto_fix.review_record_json(auto_fix.ReviewRecord("PASS", ()))
        reviewer = ScriptedAdapter([
            TaskResult(0, failed, False, None),
            TaskResult(0, passed, False, None),
        ])

        def interrupt_after_edit(_prompt):
            target = self.execution_root() / "src" / "value.txt"
            target.write_text("partial\n", encoding="utf-8")
            raise KeyboardInterrupt

        first_worker = ScriptedAdapter([interrupt_after_edit])
        self.assertEqual(self.run_quiet(
            cfg, adapter=first_worker, auto_fix_adapter=reviewer,
            auto_fix=True), 130)
        self.assertEqual(parse_task_file(task_path).status, "WIP")
        interrupted_state = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg))
        durable_brief = interrupted_state.repair_briefs[0].brief
        self.assertIn(durable_brief, first_worker.calls[0][0])

        second_worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "fixed\n"})])
        self.assertEqual(self.run_quiet(
            cfg, adapter=second_worker, auto_fix_adapter=reviewer,
            auto_fix=True), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        # An interrupted repair resumes on exactly the identity it started on:
        # nothing is consumed, because only the review-round position advances.
        attempts = [
            entry for entry in read_entries(journal_path_for(task_path))
            if entry["event"] == "auto_fix_attempt"]
        self.assertEqual(
            [(item["agent"], item["requested_model"], item["requested_effort"])
             for item in attempts],
            [("claude", "lite", "medium"), ("claude", "lite", "medium")])
        # Round 0 failed and advanced the position; round 1 passed there.
        self.assertEqual(state.review_round_index, 1)
        self.assertEqual((self.execution_root() / "src" / "value.txt").read_text(
            encoding="utf-8"), "fixed\n")
        self.assertIn(durable_brief, second_worker.calls[0][0])
        self.assertEqual(state.repair_briefs[0].brief, durable_brief)
        self.assertEqual(
            {item.fingerprint for item in state.worker_dispositions},
            {auto_fix.finding_fingerprint(finding)})

    def test_crash_after_fixer_checkpoint_restarts_with_review_not_rework(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=TWO_REVIEW_ROUNDS)

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "stale value", "review evidence")
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "fixed\n"})])
        original_phase_change = auto_fix.with_repair_phase

        def crash_before_awaiting_review(state, phase):
            if phase == "AWAITING_REVIEW":
                raise RuntimeError("simulated crash before repair re-review")
            return original_phase_change(state, phase)

        with mock.patch.object(
                auto_fix, "with_repair_phase",
                side_effect=crash_before_awaiting_review):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.run_quiet(
                    cfg, adapter=worker, auto_fix_adapter=reviewer,
                    auto_fix=True)

        crashed = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(crashed.phase, "REPAIRING")
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        # The failed round already moved the durable position forward, so the
        # restart resumes at that round instead of replaying the sequence.
        self.assertEqual(crashed.review_round_index, 1)
        checkpoint_subjects = self.subjects()

        restart_worker = ScriptedAdapter([])
        restart_reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None),
        ])
        self.assertEqual(self.run_quiet(
            cfg, adapter=restart_worker, auto_fix_adapter=restart_reviewer,
            auto_fix=True), 0)

        recovered = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(recovered.phase, "COMPLETE")
        self.assertEqual(recovered.review_round_index,
                         crashed.review_round_index)
        self.assertEqual(restart_worker.calls, [])
        self.assertEqual(len(restart_reviewer.calls), 1)
        self.assertIn("- PASS:", restart_reviewer.calls[0][0])
        self.assertEqual(self.subjects(), checkpoint_subjects)
        attempts = [
            entry for entry in read_entries(journal_path_for(task_path))
            if entry["event"] == "auto_fix_attempt"]
        self.assertEqual(len(attempts), 1)

    def test_crash_after_worker_journal_recovers_dispositions_before_recheck(self):
        task_path = self.write_task(1, status="DONE", scope=("src/",))
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=TWO_REVIEW_ROUNDS)
        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "stale value", "review evidence")
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None),
        ])
        worker = ScriptedAdapter([
            self.repair_done(task_path, {"src/value.txt": "fixed\n"})])
        real_write = auto_fix.write_auto_fix_state

        def crash_on_dispositions(path, state):
            if state.phase == "REPAIRING" and state.worker_dispositions:
                raise RuntimeError("simulated crash after disposition journal")
            return real_write(path, state)

        with mock.patch.object(
                auto_fix, "write_auto_fix_state",
                side_effect=crash_on_dispositions):
            with self.assertRaisesRegex(RuntimeError, "disposition journal"):
                self.run_quiet(
                    cfg, adapter=worker, auto_fix_adapter=reviewer,
                    auto_fix=True)

        crashed = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(crashed.phase, "REPAIRING")
        self.assertEqual(crashed.worker_dispositions, ())
        self.assertEqual(parse_task_file(task_path).status, "DONE")

        def pass_after_recovery(prompt):
            self.assertIn(auto_fix.finding_fingerprint(finding), prompt)
            self.assertIn(
                "fixed; The recovered repair now passes its focused gate.",
                prompt)
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)

        restart_reviewer = ScriptedAdapter([pass_after_recovery])
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([]),
            auto_fix_adapter=restart_reviewer, auto_fix=True), 0)
        recovered = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(len(recovered.worker_dispositions), 1)
        self.assertEqual(len(restart_reviewer.calls), 1)


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
        review = cfg.auto_fix_review[0]
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
            reviewer_adapter=review.adapter,
            reviewer_model=review.requested_model,
            reviewer_effort=review.requested_effort,
            review_round_index=1)
        if state.phase != phase:
            state = auto_fix.with_repair_phase(state, phase)
        auto_fix.write_auto_fix_state(auto_fix.auto_fix_state_path(cfg), state)

    def test_in_flight_review_round_dirt_recovers_and_the_run_continues(self):
        cfg, path, worktree = self._reviewed_folder()
        self._write_review_state(cfg)
        (worktree / "src" / "value.txt").write_text(
            "interrupted reviewer repair\n", encoding="utf-8")

        def pass_after_recovery(prompt):
            # Proven from inside the first AI session of the run: the dirt was
            # already gathered into its wip checkpoint before any session began.
            self.assertTrue(any(s.startswith("wip(plan01/t001): recovered dirty worktree")
                                for s in self.subjects()))
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)

        worker = ScriptedAdapter([])
        reviewer = ScriptedAdapter([pass_after_recovery])
        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer, auto_fix=True), 0)

        self.assertIn(
            "wip(plan01/t001): recovered dirty worktree from an interrupted "
            "review round, scope-verified", self.subjects())
        # Recovery opens no session of its own and reopens nothing: the run
        # continues into the round the interrupt left pending, and the fixer
        # side is never dispatched.
        self.assertEqual(worker.calls, [])
        self.assertEqual(len(reviewer.calls), 1)
        self.assertEqual(parse_task_file(path).status, "DONE")
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(e["event"] == "auto_fix_attempt" for e in entries))
        # The interrupted round's edits survived, never discarded.
        self.assertEqual(
            (worktree / "src" / "value.txt").read_text(encoding="utf-8"),
            "interrupted reviewer repair\n")
        # A scheduler recovery entry naming the round as the origin, with no
        # fabricated AI session identity.
        recovery = next(
            e for e in entries
            if e["by"] == "scheduler" and "Recovered a dirty worktree" in e["summary"])
        self.assertIn("an interrupted review round", recovery["summary"])
        self.assertIn("reviewer-fixer round", recovery["detail"])
        self.assertNotIn("agent", recovery)
        self.assertNotIn("requested_model", recovery)

    def _assert_refused(self, cfg, worktree):
        """The dirt reaches ensure_clean: no session, no checkpoint, nothing committed."""
        worker = ScriptedAdapter([])
        reviewer = ScriptedAdapter([])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(
                cfg, adapter=worker, auto_fix_adapter=reviewer, auto_fix=True), 1)
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
        path = self.write_task(1)  # default scope=("src/",)
        # "src/" must already be a tracked directory before the leak: otherwise git status
        # collapses a wholly-new untracked directory into a single "?? src/" line instead of
        # naming the leaked file, which this test does not exercise.
        (self.root / "src").mkdir()
        (self.root / "src" / "existing.py").write_text("existing\n", encoding="utf-8")
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
