"""engine tests for recovering work the scheduler never got to record cleanly.

Two situations, both of which must keep every produced byte: a worktree left dirty by an
unclean process exit (hard power loss / kill) that never reached an interrupt handler, and a
session that wrote into the main tree instead of its isolated worktree. Shared fixtures come
from tests.engine_support.

Chinese literals that remain are deliberate user/upstream passthrough data."""
import contextlib
import io
import subprocess
import unittest

from assent import engine, gitops
from assent.plan import journal_path_for, parse_task_file
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result


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
        """A DONE task can legitimately have no auto() commit when all of its changes were
        already stored in an earlier wip checkpoint; that clean state stays untouched."""
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


class TestMainTreeEscapeDetection(EngineTestCase):
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
