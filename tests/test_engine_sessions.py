"""engine tests for one session round: the adapter call, its process outcome, and the
quota/billing interruptions that keep the task resumable.

Every case here starts (or refuses to start) a scripted AI session and checks what the
scheduler records about it -- the resolved identity in the prompt and journal, an adapter
exit or watchdog stall, a quota round with its wait math, and an exhausted prepaid balance.
The core run loop lives in tests.test_engine; shared fixtures in tests.engine_support.

Chinese literals that remain are deliberate user/upstream passthrough data."""
import _thread
import contextlib
import io
import json
import re
import subprocess
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from assent import (AssentError, auto_fix, engine, folder_scheduler, gitops,
                    shared_paths, usage)
from assent.adapters import CHECKPOINT_RESUME_RECORD, TaskResult, TokenUsage
from assent.adapters.process import (clear_stop_wake, interruptible_sleep,
                                     run_subprocess, wake_stop_waiters)
from assent.config import load_config
from assent.plan import (Plan, append_entry, journal_path_for, parse_task_file,
                         read_entries, read_workflow_state,
                         SelectionWorkflowState, set_status,
                         workflow_state_path)
from assent.verification_common import FullVerifyEvidence
from tests.engine_support import (EngineTestCase, ScriptedAdapter, ok_result,
                                  task_text)
from tests.link_support import make_directory_link
from tests.test_contracts import GlobalContractsMixin


class TestBoundedAutoFixSession(GlobalContractsMixin, EngineTestCase):
    def test_review_prompt_states_round_and_forbids_invented_acceptance(self):
        middle = engine._auto_fix_round_policy(1, 3)
        final = engine._auto_fix_round_policy(2, 3)

        self.assertIn("review round 2 of 3", middle)
        self.assertIn("Review rounds remaining after this one: 1", middle)
        self.assertNotIn("FINAL review round", middle)
        self.assertIn("review round 3 of 3", final)
        self.assertIn("FINAL review round", final)
        self.assertIn("Never invent an acceptance criterion",
                      final.replace("\n", " "))
        self.assertIn(
            "never invent a new acceptance criterion",
            engine._AUTO_FIX_REVIEW_PROMPT.replace("\n", " "))

    @staticmethod
    def review_rounds(count, *names):
        """A workflow with exactly ``count`` verdict-producing review steps.

        The merged reviewer-fixer loop is finite because it walks this list
        position by position, so a case needing N reviewer sessions must
        configure N rounds.
        """
        adapters = list(names) or ["claude"] * count
        rendered = ', { action = "focused_sweep" }, '.join(
            f'{{ role = "folder_reviewer", adapter = {json.dumps(name)} }}'
            for name in adapters)
        return (
            '\n[abilities.review_fix]\nprompt = "Review and repair."\n'
            'writes = true\nproduces_verdict = true\n'
            '[abilities.fix]\nprompt = "Repair durable findings."\n'
            'writes = true\n'
            '[roles.folder_reviewer]\nability = ["review_fix"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[roles.bounded_fixer]\nability = ["fix"]\n'
            '[workflow]\nplan = [{ action = "focused_sweep" }, '
            f'{rendered}, {{ action = "focused_sweep" }}]\n')

    @staticmethod
    def review_session_agents(output):
        """The adapter each review round actually ran under, in order."""
        return re.findall(r"(?m)^Auto-fix review session: (\S+) \|", output)

    def repair_done(self, task_path, files=None, *, requested_model="lite",
                    by="claude"):
        def step(prompt):
            for rel, content in (files or {}).items():
                path = self.execution_root() / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            fingerprints = tuple(dict.fromkeys(re.findall(
                r"(?m)^- fingerprint: ([0-9a-f]{64})$", prompt)))
            self.assertTrue(fingerprints)
            detail = "\n".join(
                "ASSENT_REPAIR_DISPOSITION " + json.dumps({
                    "fingerprint": fingerprint,
                    "disposition": "fixed",
                    "detail": "The task-focused repair and regression pass.",
                }, separators=(",", ":"), sort_keys=True)
                for fingerprint in fingerprints)
            set_status(task_path, "DONE")
            append_entry(
                journal_path_for(task_path), by=by,
                requested_model=requested_model, event="done",
                summary="Repair completed", detail=detail)
            return ok_result()
        return step

    def test_passing_plan_action_skips_review_before_integration(self):
        task_path = self.write_task(1, status="TODO", scope=("src/",))
        cfg = self.build(extra_config=(
            '\n[abilities.write_tests]\n'
            'prompt = "Write the requirement tests."\n'
            'writes = true\n'
            '[abilities.implement_source]\n'
            'prompt = "Implement the production source."\n'
            'writes = true\n'
            '[abilities.review]\n'
            'prompt = "Review the completed plan."\n'
            'writes = false\nproduces_verdict = true\n'
            '[abilities.fix]\n'
            'prompt = "Repair reviewed findings."\n'
            'writes = true\n'
            '[roles.implementer]\n'
            'ability = ["write_tests", "implement_source"]\n'
            '[roles.reviewer_fixer]\nability = ["review", "fix"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\n'
            'task = [{ role = "implementer" }]\n'
            'plan = [{ action = "focused_sweep" }, '
            '{ role = "reviewer_fixer", adapter = "claude" }, '
            '{ action = "focused_sweep" }, '
            '{ role = "reviewer_fixer", adapter = "claude" }, '
            '{ action = "focused_sweep" }]\n'
            'integration = [{ action = "full_verify" }]\n'))
        (self.root / ".assent" / "verify.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8")
        self.commit_all()
        worker = ScriptedAdapter([
            self.ai_done(task_path, {"src/value.txt": "implemented\n"})])
        reviewer = ScriptedAdapter([TaskResult(
            0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer), 0)

        self.assertEqual(len(cfg.workflow_plan), 5)
        self.assertEqual([step.role for step in cfg.workflow_plan
                          if hasattr(step, "role")],
                         ["reviewer_fixer", "reviewer_fixer"])
        self.assertEqual(len(worker.calls), 1)
        self.assertIn("Write the requirement tests.", worker.calls[0][0])
        self.assertIn("Implement the production source.", worker.calls[0][0])
        self.assertEqual(len(reviewer.calls), 0)

        verify_calls = 0

        def verify_action(_cfg, *, recheck=False):
            nonlocal verify_calls
            verify_calls += 1
            _branch, source, _worktree = engine.source_snapshot(
                cfg, gitops.main_worktree(cfg.root))
            return FullVerifyEvidence(
                "PASSED", ("plan01",), gitops.commit_of(cfg.root, "HEAD"),
                (source,), "a" * 40,
                engine.verification.verifier_digest(cfg), "b" * 64, 0, (),
                False)

        with mock.patch("assent.engine.verify_folder_action",
                        side_effect=verify_action):
            self.assertEqual(engine.run_selection_workflow(
                str(self.root / ".assent" / "assent.toml"),
                self.root / ".assent", ["plan01"]), 0)
        self.assertEqual(verify_calls, 1)

    def test_initial_writable_plan_quality_review_repairs_before_sweep(self):
        task_path = self.write_task(
            1, status="DONE", scope=("src/value.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "value.txt").write_text("old\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(
            ".assent/\ncache/\n", encoding="utf-8")
        cfg = self.build(extra_config=(
            '\n[abilities.quality]\n'
            'prompt = "Review cumulative quality before focused_sweep."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.any_name]\nability = ["quality"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nplan = [{ role = "any_name" }, '
            '{ action = "focused_sweep" }]\n'))
        self.commit_all()
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "generated.bin").write_text("local\n", encoding="utf-8")

        def repair(prompt):
            self.assertIn(
                "Review cumulative quality before focused_sweep.", prompt)
            self.assertIn(
                "focused_sweep follows the initial plan quality review", prompt)
            self.assertIn("terminal assent.auto_fix_review JSON", prompt)
            self.assertIn("Do not run `assent shared-paths review`", prompt)
            (self.execution_root() / "src" / "value.txt").write_text(
                "repaired\n", encoding="utf-8")
            finding = auto_fix.ReviewFinding(
                "t001", "src/value.txt", "Cumulative behavior was incorrect",
                "The existing task requirement is violated by the old value.")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord(
                        "FIXED", (finding,),
                        auto_fix.SharedPathsDecision(
                            (), ("AGENTS.md",),
                            (auto_fix.SharedPathDisposition(
                                "cache", "generated cache is worktree-local"),)))),
                False, None)

        passed = subprocess.CompletedProcess([], 0, "focused pass\n", "")
        reviewer = ScriptedAdapter([repair])
        with mock.patch.object(
                engine, "_verify_subprocess", return_value=passed) as sweep:
            self.assertEqual(self.run_quiet(
                cfg, auto_fix_adapter=reviewer), 0)

        self.assertEqual(len(reviewer.calls), 1)
        sweep.assert_called_once()
        self.assertEqual(
            (self.execution_root() / "src" / "value.txt").read_text(
                encoding="utf-8"),
            "repaired\n")
        self.assertEqual(
            shared_paths.classify(self.root, self.execution_root()).state,
            shared_paths.REVIEWED_NONE)

    def test_initial_review_pass_after_source_write_is_not_persisted(self):
        self.write_task(1, status="DONE", scope=("src/value.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "value.txt").write_text("old\n", encoding="utf-8")
        cfg = self.build(extra_config=(
            '\n[abilities.quality]\n'
            'prompt = "Review cumulative quality."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.reviewer]\nability = ["quality"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nplan = [{ role = "reviewer" }, '
            '{ action = "focused_sweep" }]\n'))
        self.commit_all()

        def invalid_pass(_prompt):
            (self.execution_root() / "src" / "value.txt").write_text(
                "changed\n", encoding="utf-8")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("PASS", ())), False, None)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = engine.run(cfg, auto_fix_adapter=ScriptedAdapter([invalid_pass]))

        self.assertEqual(rc, 1)
        self.assertFalse(auto_fix.auto_fix_state_path(cfg).exists())
        self.assertIn("Auto-fix reviewer verdict rejected", output.getvalue())
        self.assertIn("source:src/value.txt", output.getvalue())
        self.assertTrue(
            gitops.working_tree_status(
                self.execution_root(), cfg.git_excludes).is_clean)
        self.assertEqual(
            (self.execution_root() / "src" / "value.txt").read_text(
                encoding="utf-8"),
            "changed\n")

    def test_pre_action_pass_replays_initial_review_and_settles_shared_paths(self):
        self.write_task(1, status="DONE", scope=("src/value.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "value.txt").write_text("done\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(
            ".assent/\ncache/\n", encoding="utf-8")
        cfg = self.build(retry=2, extra_config=(
            '\n[abilities.quality]\n'
            'prompt = "Review cumulative quality."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.reviewer]\nability = ["quality"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nplan = [{ role = "reviewer" }, '
            '{ action = "focused_sweep" }]\n'))
        self.commit_all()
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "generated.bin").write_text("local\n", encoding="utf-8")
        worktree = gitops.ensure_worktree(self.root, "plan01")
        gitops.ensure_branch(worktree, "plan01/")
        make_directory_link(worktree / "cache", cache)
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg),
            auto_fix.state_for_review(
                auto_fix.ReviewRecord("PASS", ()),
                source_tree=gitops.tree_of(cfg.root, "HEAD"),
                task_plan_sha256="a" * 64,
                review_prompt_sha256="b" * 64,
                reviewer_adapter="claude", reviewer_role="reviewer",
                reviewer_model="opus", reviewer_effort="high",
                workflow_step_index=0))

        def review_shared(prompt):
            self.assertIn("Shared ignored directories", prompt)
            self.assertIn("terminal assent.auto_fix_review JSON", prompt)
            self.assertIn("unreviewed same-primary directory link: cache", prompt)
            self.assertIn("omits existing ignored directory link", prompt)
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord(
                        "PASS", (),
                        auto_fix.SharedPathsDecision(
                            ("cache",), ("AGENTS.md",)))),
                False, None)

        passed = subprocess.CompletedProcess([], 0, "focused pass\n", "")
        missing_decision = TaskResult(
            0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)
        omitted_link = TaskResult(
            0, auto_fix.review_record_json(
                auto_fix.ReviewRecord(
                    "PASS", (),
                    auto_fix.SharedPathsDecision(
                        (), ("AGENTS.md",),
                        (auto_fix.SharedPathDisposition(
                            "cache", "generated cache is worktree-local"),)))),
            False, None)
        reviewer = ScriptedAdapter(
            [missing_decision, omitted_link, review_shared])
        with mock.patch.object(
                engine, "_verify_subprocess", return_value=passed):
            self.assertEqual(self.run_quiet(
                cfg, auto_fix_adapter=reviewer), 0)

        self.assertEqual(len(reviewer.calls), 3)
        self.assertEqual(
            shared_paths.classify(self.root, self.execution_root()).state,
            shared_paths.REVIEWED_PATHS)
        self.assertEqual(
            shared_paths.classify(
                self.root, self.execution_root()).paths,
            ("cache",))
        self.assertEqual(
            auto_fix.read_auto_fix_state(
                auto_fix.auto_fix_state_path(cfg)).workflow_step_index,
            1)

    def test_selection_verifier_failure_repairs_and_rechecks_in_one_call(self):
        task_path = self.write_task(
            1, status="TODO", scope=("src/base.txt", "src/value.txt"))
        extra = (
            '\n[abilities.review_fix]\n'
            'prompt = "Diagnose and repair the failed selection verifier."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.reviewer_fixer]\nability = ["review_fix"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nintegration = [{ action = "full_verify" }, '
            '{ role = "reviewer_fixer", adapter = ["codex", "claude"] }, '
            '{ action = "full_verify" }]\n')
        cfg = self.build(extra_config=extra)
        (self.root / ".assent" / "verify.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8")
        source = self.root / "src"
        source.mkdir()
        (source / "base.txt").write_text("base\n", encoding="utf-8")
        (source / "value.txt").write_text("initial\n", encoding="utf-8")
        self.commit_all()
        worker = ScriptedAdapter([
            self.ai_done(task_path, {
                "src/value.txt": "broken\n",
            })])
        self.assertEqual(self.run_quiet(cfg, adapter=worker), 0)
        task_path.write_text(
            task_text(status="DONE", scope=("src/base.txt",)),
            encoding="utf-8", newline="\n")

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "The selected candidate is broken",
            "The complete verifier reported the wrong value.",
            kind="scope_amendment",
            recommendation="Repair and append this exact existing source file.",
            scope_addition=auto_fix.ScopeAddition(
                "src/value.txt", "existing_file"))
        def review_and_fix(prompt):
            self.assertIn("write-capable merged review-and-repair", prompt)
            self.assertIn(str((self.execution_root() / "AGENTS.md").resolve()),
                          prompt)
            self.assertIn(str(engine.contracts.instructions_path()), prompt)
            self.assertIn(str(self.execution_root()), prompt)
            (self.execution_root() / "src" / "value.txt").write_text(
                "fixed\n", encoding="utf-8")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("FIXED", (finding,))), False, None,
                usage=(TokenUsage(provider_model="claude-review",
                                  input_tokens=10, output_tokens=2),))

        reviewer_and_fixer = ScriptedAdapter([review_and_fix])
        unavailable = ScriptedAdapter([TaskResult(
            1, "Not logged in", False, None,
            failure_kind="authentication")])
        calls = 0
        rechecks = []

        def verify_action(_cfg, *, recheck=False):
            nonlocal calls
            calls += 1
            rechecks.append(recheck)
            _branch, source, _worktree = engine.source_snapshot(
                cfg, gitops.main_worktree(cfg.root))
            return FullVerifyEvidence(
                "VERIFIER_FAILED" if calls == 1 else "PASSED",
                ("plan01",), gitops.commit_of(cfg.root, "HEAD"), (source,),
                ("a" if calls == 1 else "b") * 40,
                engine.verification.verifier_digest(cfg), "c" * 64,
                1 if calls == 1 else 0,
                ("src/value.txt failed",) if calls == 1 else (), False)

        with mock.patch(
                "assent.engine.get_adapter",
                side_effect=lambda name, _cfg: (
                    unavailable if name == "codex" else reviewer_and_fixer)), \
                mock.patch("assent.engine.verify_folder_action",
                           side_effect=verify_action):
            self.assertEqual(engine.run_selection_workflow(
                str(self.root / ".assent" / "assent.toml"),
                self.root / ".assent", ["plan01"]), 0)

        self.assertEqual(calls, 2)
        self.assertEqual(len(unavailable.calls), 1)
        self.assertEqual(len(reviewer_and_fixer.calls), 1)
        self.assertEqual(rechecks, [False, True])
        self.assertEqual(parse_task_file(task_path).status, "DONE")
        self.assertEqual(
            parse_task_file(task_path).scope,
            ["src/base.txt", "src/value.txt"])
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.review_context, "selection_verification")
        self.assertEqual(state.verdict, "PASS")
        self.assertEqual(
            (self.execution_root() / "src" / "value.txt").read_text(
                encoding="utf-8"), "fixed\n")
        records, invalid = usage.read_records(cfg.assent_dir)
        selection = [record for record in records
                     if record["context"]["kind"] == "selection"]
        self.assertEqual(invalid, 0)
        self.assertEqual(len(selection), 2)
        self.assertTrue(all(record["folders"] == ["plan01"]
                            for record in selection))
        reviewed = next(record for record in selection if record["models"])
        self.assertEqual(reviewed["models"][0]["provider_model"],
                         "claude-review")

    def test_selection_target_conflict_resumes_ai_reconcile_before_full_verify(self):
        task_path = self.write_task(
            1, status="DONE", scope=("src/value.txt",))
        source = self.root / "src"
        source.mkdir()
        (source / "value.txt").write_text("base\n", encoding="utf-8")
        extra = (
            '\n[abilities.review_fix]\n'
            'prompt = "Assign and resolve every selection conflict path."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.reviewer_fixer]\nability = ["review_fix"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nintegration = [{ action = "full_verify" }, '
            '{ role = "reviewer_fixer", adapter = "codex" }, '
            '{ action = "full_verify" }]\n')
        cfg = self.build(extra_config=extra)
        (self.root / ".assent" / "verify.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8")
        self.commit_all()
        worktree = gitops.ensure_worktree(self.root, "plan01")
        branch = gitops.ensure_branch(worktree, "plan01/")
        (worktree / "src" / "value.txt").write_text(
            "from source\n", encoding="utf-8")
        gitops.commit_all(worktree, "source change")
        source_tip = gitops.branch_tip(self.root, branch)
        (self.root / "src" / "value.txt").write_text(
            "from target\n", encoding="utf-8")
        self.commit_all("target change")
        target_tip = gitops.commit_of(self.root, "HEAD")

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "Selection target conflict",
            "The source and target both changed the task-owned path.")

        def resolve(prompt):
            self.assertIn("write-capable merged review-and-repair", prompt)
            managed = gitops.reconcile_worktree_path(self.root, "plan01")
            self.assertIn(str((self.execution_root() / "AGENTS.md").resolve()),
                          prompt)
            self.assertIn(str(engine.contracts.instructions_path()), prompt)
            self.assertIn(str(managed), prompt)
            (managed / "src" / "value.txt").write_text(
                "resolved automatically\n", encoding="utf-8")
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FIXED", (finding,))), False, None)

        reviewer_and_fixer = ScriptedAdapter([resolve])
        requested_adapters = []

        def configured_adapter(name, _cfg):
            requested_adapters.append(name)
            return reviewer_and_fixer

        with mock.patch("assent.engine.get_adapter",
                        side_effect=configured_adapter), mock.patch(
                "assent.engine.reconcile.automatic_reconcile_continue_locked",
                side_effect=AssentError("simulated closeout interruption")):
            self.assertEqual(engine.run_selection_workflow(
                str(self.root / ".assent" / "assent.toml"),
                self.root / ".assent", ["plan01"]), 1)

        def approve_existing(prompt):
            managed = gitops.reconcile_worktree_path(self.root, "plan01")
            self.assertEqual(
                (managed / "src" / "value.txt").read_text(encoding="utf-8"),
                "resolved automatically\n")
            data = json.loads(auto_fix.review_record_json(
                auto_fix.ReviewRecord("FIXED", (finding,))))
            data["findings"][0]["kind"] = "target_alone"
            return TaskResult(0, json.dumps(data), False, None)

        resumed_reviewer = ScriptedAdapter([approve_existing])
        with mock.patch("assent.engine.get_adapter",
                        return_value=resumed_reviewer), mock.patch(
                "assent.engine._verify_focused_locked", return_value=1):
            code = engine.run_selection_workflow(
                str(self.root / ".assent" / "assent.toml"),
                self.root / ".assent", ["plan01"])

        self.assertEqual(code, 1)
        with mock.patch(
                "assent.engine.get_adapter",
                side_effect=AssertionError(
                    "a persisted merged repair must not rerun its reviewer")):
            code = engine.run_selection_workflow(
                str(self.root / ".assent" / "assent.toml"),
                self.root / ".assent", ["plan01"])

        self.assertEqual(code, 0)
        resolved_tip = gitops.branch_tip(self.root, branch)
        self.assertEqual(
            gitops.commit_parents(self.root, resolved_tip),
            (source_tip, target_tip))
        self.assertEqual(gitops.commit_of(self.root, "HEAD"), target_tip)
        self.assertEqual(len(reviewer_and_fixer.calls), 1)
        self.assertEqual(len(resumed_reviewer.calls), 1)
        self.assertIn("codex", requested_adapters)
        self.assertIn("do not run tests", reviewer_and_fixer.calls[0][0].lower())
        self.assertEqual(parse_task_file(task_path).status, "DONE")

    def test_selection_focused_failure_reviews_a_named_missing_shared_input(self):
        self.write_task(1, status="DONE")
        (self.root / ".gitignore").write_text(
            ".assent/\ncache/\n", encoding="utf-8")
        extra = (
            '\n[abilities.integration_review]\n'
            'prompt = "Review exact integration evidence."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.integration_reviewer]\n'
            'ability = ["integration_review"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nintegration = [{ action = "full_verify" }, '
            '{ role = "integration_reviewer", adapter = "claude" }, '
            '{ action = "full_verify" }]\n')
        cfg = self.build(extra_config=extra)
        self.commit_all()
        worktree = gitops.ensure_worktree(self.root, "plan01")
        source_cfg = cfg.for_worktree(worktree)
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "generated.dart").write_text(
            "generated\n", encoding="utf-8")
        shared_paths.review(
            self.root, worktree, none=True, watch=("AGENTS.md",),
            dispositions=(shared_paths.PathDisposition(
                "cache", "generated cache is worktree-local"),))

        record = auto_fix.ReviewRecord(
            "PASS", (), auto_fix.SharedPathsDecision(
                ("cache",), ("AGENTS.md",)))
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(record), False, None)])
        state = SelectionWorkflowState(
            ("plan01",), "master", gitops.commit_of(self.root, "HEAD"),
            (gitops.commit_of(worktree, "HEAD"),), 1,
            action="full_verify", action_status="FAILED",
            action_candidate_tree="a" * 40, action_exit_code=1,
            action_evidence=("VERIFIER_FAILED",))
        step = cfg.workflow_integration[1]

        with mock.patch("assent.engine.get_adapter", return_value=reviewer):
            recovered = engine._recover_selection_focused_shared_paths(
                source_cfg, state, step,
                "Error reading cache/generated.dart: file not found",
                sleep=lambda _seconds: None,
                now=lambda: datetime.now(timezone.utc))

        self.assertTrue(recovered)
        self.assertEqual(
            shared_paths.classify(self.root, worktree).paths, ("cache",))
        shared_paths.require_directory_link_agreement(
            self.root, worktree,
            shared_paths.classify(self.root, worktree))
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIn("cache", reviewer.calls[0][0])

    def test_selection_reviews_a_profile_switch_before_focused_closeout(self):
        self.write_task(1, status="DONE")
        (self.root / ".gitignore").write_text(
            ".assent/\ncache/\n", encoding="utf-8")
        (self.root / "deps.txt").write_text("first\n", encoding="utf-8")
        extra = (
            '\n[abilities.integration_review]\n'
            'prompt = "Review exact integration evidence."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.integration_reviewer]\n'
            'ability = ["integration_review"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nintegration = [{ action = "full_verify" }, '
            '{ role = "integration_reviewer", adapter = "claude" }, '
            '{ action = "full_verify" }]\n')
        cfg = self.build(extra_config=extra)
        self.commit_all()
        worktree = gitops.ensure_worktree(self.root, "plan01")
        source_cfg = cfg.for_worktree(worktree)
        cache = self.root / "cache"
        cache.mkdir()
        (cache / "generated.dart").write_text(
            "generated\n", encoding="utf-8")

        shared_paths.review(
            self.root, worktree, paths=("cache",), watch=("deps.txt",))
        (worktree / "deps.txt").write_text("second\n", encoding="utf-8")
        shared_paths.review(
            self.root, worktree, none=True, watch=("deps.txt",),
            dispositions=(shared_paths.PathDisposition(
                "cache", "generated cache is worktree-local"),))
        (worktree / "deps.txt").write_text("first\n", encoding="utf-8")
        shared_paths.prepare_worktree(self.root, worktree)
        (worktree / "deps.txt").write_text("second\n", encoding="utf-8")
        self.assertEqual(
            shared_paths.prepare_worktree(self.root, worktree).state,
            shared_paths.STALE)

        record = auto_fix.ReviewRecord(
            "PASS", (), auto_fix.SharedPathsDecision(
                ("cache",), ("deps.txt",)))
        reviewer = ScriptedAdapter([
            TaskResult(0, auto_fix.review_record_json(record), False, None)])
        state = SelectionWorkflowState(
            ("plan01",), "master", gitops.commit_of(self.root, "HEAD"),
            (gitops.commit_of(worktree, "HEAD"),), 1,
            action="full_verify", action_status="FAILED",
            action_candidate_tree="a" * 40, action_exit_code=1,
            action_evidence=("TARGET_CONFLICT",))
        step = cfg.workflow_integration[1]

        with mock.patch("assent.engine.get_adapter", return_value=reviewer):
            engine._recover_pending_selection_shared_paths(
                (source_cfg,), state, step,
                sleep=lambda _seconds: None,
                now=lambda: datetime.now(timezone.utc))

        settled = shared_paths.classify(self.root, worktree)
        self.assertEqual(settled.state, shared_paths.REVIEWED_PATHS)
        self.assertEqual(settled.paths, ("cache",))
        shared_paths.require_directory_link_agreement(
            self.root, worktree, settled)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertIn("Worktree preparation found", reviewer.calls[0][0])

    def test_selection_merged_fail_without_edits_skips_final_full_verify(self):
        task_path = self.write_task(1, status="TODO", scope=("src/",))
        cfg = self.build(extra_config=(
            '\n[abilities.review_fix]\n'
            'prompt = "Diagnose and repair the failed selection verifier."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.reviewer_fixer]\nability = ["review_fix"]\n'
            'model = "prime"\neffort = "heavy"\n'
            '[workflow]\nintegration = [{ action = "full_verify" }, '
            '{ role = "reviewer_fixer", adapter = "claude" }, '
            '{ action = "full_verify" }]\n'))
        (self.root / ".assent" / "verify.py").write_text(
            "raise SystemExit(0)\n", encoding="utf-8")
        self.commit_all()
        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([self.ai_done(
                task_path, {"src/value.txt": "broken\n"})])), 0)
        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "Repair could not be completed",
            "The complete verifier still reports the wrong value.")
        reviewer = ScriptedAdapter([TaskResult(
            0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FAIL", (finding,))), False, None)])
        verify_calls = 0

        def verify_action(_cfg, *, recheck=False):
            nonlocal verify_calls
            verify_calls += 1
            _branch, source, _worktree = engine.source_snapshot(
                cfg, gitops.main_worktree(cfg.root))
            return FullVerifyEvidence(
                "VERIFIER_FAILED", ("plan01",),
                gitops.commit_of(cfg.root, "HEAD"), (source,), "a" * 40,
                engine.verification.verifier_digest(cfg), "b" * 64, 1,
                ("src/value.txt failed",), False)

        with mock.patch("assent.engine.get_adapter", return_value=reviewer), \
                mock.patch("assent.engine.verify_folder_action",
                           side_effect=verify_action):
            self.assertEqual(engine.run_selection_workflow(
                str(self.root / ".assent" / "assent.toml"),
                self.root / ".assent", ["plan01"]), 0)

        self.assertEqual(verify_calls, 1)
        self.assertEqual(len(reviewer.calls), 1)

    def test_repair_gate_failure_preserves_reviewer_step_identity(self):
        task_path = self.write_task(1, status="DONE")
        cfg = self.build(extra_config=self.review_rounds(1))
        self.commit_all()
        plan = Plan.parse(cfg.tasks_dir)
        task = parse_task_file(task_path)
        review = cfg.workflow_plan[1]
        finding = auto_fix.ReviewFinding(
            task.id, "src/value.txt", "Repair is incomplete",
            "The task-focused gate failed after repair.")
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (finding,)),
            source_tree=gitops.tree_of(cfg.root, "HEAD"),
            task_plan_sha256=auto_fix.sha256_files(
                item.path for item in plan.tasks),
            review_prompt_sha256="a" * 64,
            reviewer_role=review.role, reviewer_step_index=0,
            reviewer_adapter=review.adapter,
            reviewer_model=review.requested_model,
            reviewer_effort=review.requested_effort,
            workflow_step_index=1)

        failed = engine._auto_fix_failure_state(
            cfg, state, [(task, "focused gate failed")])

        self.assertEqual(failed.reviewer_role, review.role)
        self.assertEqual(failed.reviewer_step_index, 0)
        reconciled, note = engine._reconcile_auto_fix_recovery_config(
            cfg, failed)
        self.assertEqual(reconciled, failed)
        self.assertIsNone(note)

    def test_changed_plan_review_order_restarts_cursor_and_keeps_findings(self):
        task_path = self.write_task(1, status="DONE")
        roles = (
            '[abilities.first_review]\n'
            'prompt = "First review."\n'
            'writes = false\nproduces_verdict = true\n'
            '[abilities.second_review]\n'
            'prompt = "Second review."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.first_reviewer]\n'
            'ability = ["first_review"]\nmodel = "core"\n'
            '[roles.second_reviewer]\n'
            'ability = ["second_review"]\nmodel = "prime"\n')
        cfg = self.build(extra_config=(
            roles + '[workflow]\nplan = ['
            '{ role = "first_reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }, '
            '{ role = "second_reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }]\n'))
        self.commit_all()
        plan = Plan.parse(cfg.tasks_dir)
        review = tuple(
            step for step in cfg.workflow_plan
            if isinstance(step, engine.WorkflowPlanStep))[1]
        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "Pending finding", "Durable evidence.")
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (finding,)),
            source_tree=gitops.tree_of(cfg.root, "HEAD"),
            task_plan_sha256=auto_fix.sha256_files(
                item.path for item in plan.tasks),
            review_prompt_sha256="a" * 64,
            reviewer_role=review.role, reviewer_step_index=1,
            reviewer_adapter=review.adapter,
            reviewer_model=review.requested_model,
            reviewer_effort=review.requested_effort,
            workflow_step_index=2)
        drifted = self.build(extra_config=(
            roles + '[workflow]\nplan = ['
            '{ role = "second_reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }, '
            '{ role = "first_reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }]\n'))

        restarted, note = engine._reconcile_auto_fix_recovery_config(
            drifted, state)

        self.assertIsNotNone(note)
        self.assertEqual(restarted.workflow_step_index, 0)
        self.assertEqual(restarted.reviewer_step_index, 1)
        self.assertEqual(restarted.findings, state.findings)
        self.assertEqual(restarted.current_finding_fingerprints,
                         state.current_finding_fingerprints)

        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(drifted), state)
        reviewer = ScriptedAdapter([TaskResult(
            0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("PASS", ())), False, None)])
        self.assertEqual(self.run_quiet(
            drifted, auto_fix_adapter=reviewer), 0)
        self.assertEqual(len(reviewer.calls), 1)
        settled = auto_fix.read_auto_fix_state(
            auto_fix.auto_fix_state_path(drifted))
        self.assertEqual(settled.verdict, "PASS")
        self.assertEqual(settled.reviewer_role, "second_reviewer")

    @staticmethod
    def recheck_record(finding):
        fingerprint = auto_fix.finding_fingerprint(finding)
        return auto_fix.review_record_json(auto_fix.ReviewRecord("FAIL", (
            auto_fix.ReviewFinding(
                finding.task_id, finding.path, finding.summary,
                finding.evidence, kind=finding.kind,
                recommendation=finding.recommendation,
                scope_addition=finding.scope_addition,
                transition="still_present",
                prior_fingerprint=fingerprint,
                transition_evidence="The repair diff still reproduces the issue."),
        )))







    def test_plan_verdict_repairs_scope_omission_in_same_session(self):
        task_path = self.write_task(
            1, scope=("src/base.py",), status="DONE")
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        (source / "needed.py").write_text("value = 1\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(1))

        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py", "Required source file was omitted from scope",
            "The blocked worker identified src/needed.py as the exact required edit.",
            kind="scope_amendment",
            recommendation="Append the exact existing file to t001 scope.",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "existing_file"),
            transition="newly_exposed",
            transition_evidence=(
                "Task t001 acceptance requires the exact path exposed by the "
                "scheduler-owned focused_sweep."))

        def repair(prompt):
            self.assertIn("repair it now", prompt)
            (self.execution_root() / "src" / "needed.py").write_text(
                "value = 2\n", encoding="utf-8")
            return TaskResult(0, auto_fix.review_record_json(
                auto_fix.ReviewRecord("FIXED", (finding,))), False, None)

        reviewer = ScriptedAdapter([repair])
        focused_results = [
            subprocess.CompletedProcess([], 3, "focused failure\n", ""),
            subprocess.CompletedProcess([], 0, "focused pass\n", ""),
        ]
        with mock.patch.object(
                engine, "_verify_subprocess", side_effect=focused_results):
            self.assertEqual(self.run_quiet(
                cfg, adapter=ScriptedAdapter([]),
                auto_fix_adapter=reviewer), 0)

        task = parse_task_file(task_path)
        self.assertEqual(task.status, "DONE")
        self.assertEqual(task.scope, ["src/base.py", "src/needed.py"])
        entries = read_entries(journal_path_for(task_path))
        amendment = next(
            item for item in entries
            if item["event"] == "auto_fix_scope_amendment")
        self.assertIn("task contract before sha256", amendment["detail"])
        self.assertIn("task plan after sha256", amendment["detail"])
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        self.assertEqual(state.verdict, "PASS")
        self.assertTrue(state.plan_digest_transitions)
        self.assertEqual(len(reviewer.calls), 1)
        self.assertFalse(any(
            item.get("event") == "rework_requested" for item in entries))


    def test_scope_amendment_resumes_after_task_write_before_state_write(self):
        task_path = self.write_task(
            1, scope=("src/base.py",), status="BLOCKED")
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        (source / "needed.py").write_text("value = 1\n", encoding="utf-8")
        cfg = self.build(extra_config=self.review_rounds(1))
        plan = Plan.parse(cfg.tasks_dir)
        contracts = engine._task_contract_snapshots(plan)
        plan_digest = engine._contracts_digest(plan, contracts)
        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py", "Scope omitted an exact source file",
            "Durable worker evidence names the required source file.",
            kind="scope_amendment",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "existing_file"))
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (finding,)),
            source_tree="1" * 40, task_plan_sha256=plan_digest,
            review_prompt_sha256="2" * 64,
            reviewer_adapter="claude", reviewer_model="prime",
            reviewer_effort="heavy", review_context="blocked_adjudication",
            failure_trigger="worker_blocked")
        state_path = auto_fix.auto_fix_state_path(cfg)
        auto_fix.write_auto_fix_state(state_path, state)
        now = lambda: datetime(2026, 8, 3, tzinfo=timezone.utc)

        task_before = task_path.read_bytes()
        state_before = state_path.read_bytes()
        real_access = engine.os.access
        with mock.patch(
                "assent.engine.os.access",
                side_effect=lambda path, mode: (
                    False if Path(path) == journal_path_for(task_path).parent
                    else real_access(path, mode))):
            with self.assertRaisesRegex(AssentError, "not writable"):
                engine._apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts, now)
        self.assertEqual(task_path.read_bytes(), task_before)
        self.assertEqual(state_path.read_bytes(), state_before)

        real_write_state = auto_fix.write_auto_fix_state
        state_writes = 0

        def fail_after_transaction_record(path, value):
            nonlocal state_writes
            state_writes += 1
            if state_writes == 2:
                raise AssentError("injected state boundary")
            return real_write_state(path, value)

        with mock.patch(
                "assent.engine.auto_fix.write_auto_fix_state",
                side_effect=fail_after_transaction_record):
            with self.assertRaisesRegex(AssentError, "injected state boundary"):
                engine._apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts, now)
        self.assertIn("src/needed.py", parse_task_file(task_path).scope)
        self.assertFalse(any(
            item.get("event") == "auto_fix_scope_amendment"
            for item in read_entries(journal_path_for(task_path))))

        recovered = auto_fix.read_auto_fix_state(state_path)
        with mock.patch(
                "assent.engine.append_entry",
                side_effect=AssentError("injected journal boundary")):
            with self.assertRaisesRegex(AssentError, "injected journal boundary"):
                engine._apply_reviewed_scope_amendments(
                    cfg, recovered, Plan.parse(cfg.tasks_dir),
                    engine._task_contract_snapshots(
                        Plan.parse(cfg.tasks_dir)), now)
        recovered = auto_fix.read_auto_fix_state(state_path)
        self.assertEqual(recovered.task_plan_sha256,
                         recovered.plan_digest_transitions[-1].after_sha256)
        self.assertFalse(any(
            item.get("event") == "auto_fix_scope_amendment"
            for item in read_entries(journal_path_for(task_path))))

        recovered, _plan, _contracts = engine._apply_reviewed_scope_amendments(
            cfg, recovered, Plan.parse(cfg.tasks_dir),
            engine._task_contract_snapshots(Plan.parse(cfg.tasks_dir)), now)
        entries = [
            item for item in read_entries(journal_path_for(task_path))
            if item.get("event") == "auto_fix_scope_amendment"]
        self.assertEqual(len(entries), 1)

        engine._apply_reviewed_scope_amendments(
            cfg, recovered, Plan.parse(cfg.tasks_dir),
            engine._task_contract_snapshots(Plan.parse(cfg.tasks_dir)), now)
        entries = [
            item for item in read_entries(journal_path_for(task_path))
            if item.get("event") == "auto_fix_scope_amendment"]
        self.assertEqual(len(entries), 1)

    def test_scope_amendment_resumes_after_one_of_two_journals(self):
        first_path = self.write_task(
            1, scope=("src/base.py",), status="BLOCKED")
        second_path = self.write_task(
            2, scope=("tests/base.py",), status="BLOCKED")
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "base.py").write_text("base = 1\n", encoding="utf-8")
        (self.root / "tests" / "base.py").write_text("base = 1\n", encoding="utf-8")
        (self.root / "src" / "needed.py").write_text("value = 1\n", encoding="utf-8")
        (self.root / "tests" / "needed.py").write_text("value = 1\n", encoding="utf-8")
        cfg = self.build(extra_config=self.review_rounds(1))
        plan = Plan.parse(cfg.tasks_dir)
        contracts = engine._task_contract_snapshots(plan)
        plan_digest = engine._contracts_digest(plan, contracts)
        findings = (
            auto_fix.ReviewFinding(
                "t001", "src/needed.py", "First exact scope omission",
                "Durable evidence names src/needed.py.", kind="scope_amendment",
                scope_addition=auto_fix.ScopeAddition(
                    "src/needed.py", "existing_file")),
            auto_fix.ReviewFinding(
                "t002", "tests/needed.py", "Second exact scope omission",
                "Durable evidence names tests/needed.py.", kind="scope_amendment",
                scope_addition=auto_fix.ScopeAddition(
                    "tests/needed.py", "existing_file")),
        )
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", findings), source_tree="1" * 40,
            task_plan_sha256=plan_digest, review_prompt_sha256="2" * 64,
            reviewer_adapter="claude", reviewer_model="prime",
            reviewer_effort="heavy", review_context="blocked_adjudication",
            failure_trigger="worker_blocked")
        state_path = auto_fix.auto_fix_state_path(cfg)
        auto_fix.write_auto_fix_state(state_path, state)
        now = lambda: datetime(2026, 8, 3, tzinfo=timezone.utc)
        real_append = engine.append_entry
        appends = 0

        def fail_after_first_journal(*args, **kwargs):
            nonlocal appends
            appends += 1
            if appends == 2:
                raise AssentError("injected second journal boundary")
            return real_append(*args, **kwargs)

        with mock.patch(
                "assent.engine.append_entry", side_effect=fail_after_first_journal):
            with self.assertRaisesRegex(AssentError, "second journal boundary"):
                engine._apply_reviewed_scope_amendments(
                    cfg, state, plan, contracts, now)

        recovered = auto_fix.read_auto_fix_state(state_path)
        self.assertEqual(len(recovered.scope_amendments), 2)
        self.assertEqual(recovered.task_plan_sha256,
                         recovered.plan_digest_transitions[-1].after_sha256)
        engine._apply_reviewed_scope_amendments(
            cfg, recovered, Plan.parse(cfg.tasks_dir),
            engine._task_contract_snapshots(Plan.parse(cfg.tasks_dir)), now)
        self.assertIn("src/needed.py", parse_task_file(first_path).scope)
        self.assertIn("tests/needed.py", parse_task_file(second_path).scope)
        amendment_entries = [
            item for path in (first_path, second_path)
            for item in read_entries(journal_path_for(path))
            if item.get("event") == "auto_fix_scope_amendment"]
        self.assertEqual(len(amendment_entries), 2)

    def test_directory_scoped_focused_failure_persists_and_starts_fixer(self):
        verify = ('python -c "import pathlib,sys;sys.exit('
                  "0 if pathlib.Path('src/ok.txt').exists() else 1)\"")
        task_path = self.write_task(
            1, status="DONE", scope=("src/",), verify=verify)
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(1))

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
        worker = ScriptedAdapter([])

        self.assertEqual(self.run_quiet(
            cfg, adapter=worker, auto_fix_adapter=reviewer), 0)
        state = auto_fix.read_auto_fix_state(auto_fix.auto_fix_state_path(cfg))
        focused = next(
            finding for finding in state.findings
            if finding.summary == "Final focused verification failed")
        self.assertEqual(focused.path, "src")
        self.assertEqual(state.phase, "COMPLETE")
        self.assertEqual(len(worker.calls), 0)
        self.assertEqual(len(reviewer.calls), 1)







    def gate_log(self):
        """An absolute log a real focused gate appends to, outside every worktree."""
        log = self.root.parent / f"{self.root.name}.gatelog"
        self.addCleanup(log.unlink, True)
        return log

    @staticmethod
    def recording_gate(log, *, fails_on=None):
        """A real verify command that records the source value it was run against.

        Nothing here is mocked, so each recorded line is proof the scheduler
        really executed the task's own gate, and the value it read is proof of
        which tree that execution proved.
        """
        check = (f"sys.exit(3 if value.strip() == {fails_on!r} else 0)"
                 if fails_on is not None else "sys.exit(0)")
        return ('python -c "import pathlib,sys;'
                "value=pathlib.Path('src/value.txt').read_text(encoding='utf-8');"
                f"log=pathlib.Path(r'{log}');"
                "prev=log.read_text(encoding='utf-8') if log.exists() else '';"
                "log.write_text(prev+value,encoding='utf-8');"
                f'{check}"')

    @staticmethod
    def gate_runs(log):
        """The source value every recorded gate execution saw, in order."""
        if not log.exists():
            return []
        return log.read_text(encoding="utf-8").splitlines()

    def self_fixed_folder(self, rounds, *, verify=None, repairs=None):
        """A folder whose every configured round repairs and none confirms.

        The last round leaves a FIXED verdict with no round left to review it,
        which is the exact SELF-FIXED, UNREVIEWED hand-off point.  `verify`
        declares the task's own focused gate and `repairs` what each round
        writes, which is what the settling gate is judged on.
        """
        gate = {"verify": verify} if verify is not None else {}
        task_path = self.write_task(1, status="DONE", scope=("src/",), **gate)
        source = self.root / "src" / "value.txt"
        source.parent.mkdir()
        source.write_text("old\n", encoding="utf-8")
        self.commit_all()
        cfg = self.build(extra_config=self.review_rounds(rounds))

        finding = auto_fix.ReviewFinding(
            "t001", "src/value.txt", "value is stale",
            "The focused behavior still reads the stale value.",
            recommendation="Write the repaired value.")
        repaired = auto_fix.ReviewFinding(
            finding.task_id, finding.path, finding.summary, finding.evidence,
            kind=finding.kind, recommendation=finding.recommendation,
            transition="still_present",
            prior_fingerprint=auto_fix.finding_fingerprint(finding),
            transition_evidence="The same blocker was repaired again in place.")

        def fix(position):
            def step(_prompt):
                text = (repairs[position - 1] if repairs is not None
                        else f"repair {position}")
                (self.execution_root() / "src" / "value.txt").write_text(
                    f"{text}\n", encoding="utf-8")
                return TaskResult(0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord(
                        "FIXED", (finding if position == 1 else repaired,))),
                    False, None)
            return step

        reviewer = ScriptedAdapter([fix(index + 1) for index in range(rounds)])
        return cfg, task_path, reviewer, finding










class TestProviderUsageRecording(GlobalContractsMixin, EngineTestCase):
    def test_task_retry_records_distinct_invocations(self):
        task = self.write_task(1)
        cfg = self.build(retry=1)
        self.commit_all()

        first = TaskResult(0, "", False, None, usage=(
            TokenUsage(provider_model="model-a", input_tokens=3),))

        def finish(prompt):
            result = self.ai_done(task)(prompt)
            result.usage = (TokenUsage(provider_model="model-a",
                                       output_tokens=4),)
            return result

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = engine.run(
                cfg, once=True, adapter=ScriptedAdapter([first, finish]))
        self.assertEqual(result, 0, out.getvalue())
        records, invalid = usage.read_records(cfg.assent_dir)
        task_records = [record for record in records
                        if record["context"] == {"kind": "task", "id": "t001"}]
        self.assertEqual(invalid, 0)
        self.assertEqual(len(task_records), 2)
        self.assertEqual(len({item["invocation_id"] for item in task_records}), 2)

    def test_usage_write_failure_does_not_change_task_outcome(self):
        task = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()
        with mock.patch("assent.engine.usage.record_invocation",
                        side_effect=OSError("telemetry disk unavailable")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                result = engine.run(
                    cfg, once=True,
                    adapter=ScriptedAdapter([self.ai_done(task)]))
        self.assertEqual(result, 0, out.getvalue())
        self.assertEqual(parse_task_file(task).status, "DONE")

    def test_concurrent_records_are_complete_and_duplicate_identity_is_idempotent(self):
        cfg = self.build()
        identities = [f"session-{index}" for index in range(12)]
        threads = [threading.Thread(
            target=usage.record_invocation,
            kwargs=dict(
                assent_dir=cfg.assent_dir, invocation_id=identity,
                adapter="claude", requested_model="requested",
                context_kind="task", context_id="t001",
                folders=("plan01",),
                evidence=(TokenUsage(input_tokens=1),)))
            for identity in identities]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        usage.record_invocation(
            cfg.assent_dir, invocation_id=identities[0], adapter="claude",
            requested_model="requested", context_kind="task",
            context_id="t001", folders=("plan01",),
            evidence=(TokenUsage(input_tokens=99),))
        records, invalid = usage.read_records(cfg.assent_dir)
        self.assertEqual(invalid, 0)
        self.assertEqual(len(records), len(identities))
        self.assertTrue(all(record["models"][0]["input_tokens"] == 1
                            for record in records))


class TestWorkflowAccountabilityUnit(GlobalContractsMixin, EngineTestCase):
    TASK_ROLES = (
        '\n[abilities.prepare]\nprompt = "Prepare the implementation."\n'
        'writes = true\n'
        '[abilities.implement]\nprompt = "Implement and verify."\n'
        'writes = true\n'
        '[roles.preparer]\nability = ["prepare"]\n'
        '[roles.implementer]\nability = ["prepare", "implement"]\n'
        '[workflow]\ntask = [{ role = "preparer" }, '
        '{ role = "implementer" }]\n')
    PLAN_ROLE = (
        '\n[abilities.implement_plan]\nprompt = "Implement the whole plan."\n'
        'writes = true\n'
        '[roles.plan_worker]\nability = ["implement_plan"]\n'
        'model = "lite"\n'
        '[workflow]\ntask = []\nplan = [{ role = "plan_worker" }]\n')
    ACTION_AGENT = (
        '\n[abilities.work]\nprompt = "Work from the supplied evidence."\n'
        'writes = true\n'
        '[roles.worker]\nability = ["work"]\n')
    VERDICT_AGENT = (
        '\n[abilities.review_fix]\n'
        'prompt = "Review and repair the failed focused test."\n'
        'writes = true\nproduces_verdict = true\n'
        '[roles.task_repair]\nability = ["review_fix"]\n')

    @staticmethod
    def set_task_workflow(path, roles):
        rendered = ", ".join(
            f'{{ role = {json.dumps(role)} }}' for role in roles)
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace('status = ', f'workflow = [{rendered}]\nstatus = ', 1),
            encoding="utf-8", newline="\n")

    def install_full_verifier(self):
        script = self.root / ".assent" / "verify.py"
        script.write_text("raise SystemExit(0)\n", encoding="utf-8")
        return script.resolve()

    def test_task_roles_run_as_separate_sessions(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()

        def prepare(_prompt):
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="progress",
                         summary="Preparation completed")
            return ok_result()

        adapter = ScriptedAdapter([prepare, self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 2)
        self.assertIn("scheduled role: preparer", adapter.calls[0][0])
        self.assertIn("scheduled role: implementer", adapter.calls[1][0])
        self.assertEqual(len([
            item for item in read_entries(journal_path_for(path))
            if item.get("by") != "scheduler"]), 2)
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_task_role_string_uses_only_its_fixed_adapter(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", adapter = "codex" }]\n'))
        self.commit_all()
        claude = ScriptedAdapter([])
        codex = ScriptedAdapter([
            self.ai_done(path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=claude), 0)

        self.assertEqual(len(claude.calls), 0)
        self.assertEqual(len(codex.calls), 1)

    def test_task_role_adapter_list_fails_over_without_task_retry(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", '
              'adapter = ["codex", "claude"] }]\n'))
        self.commit_all()
        codex = ScriptedAdapter([TaskResult(
            1, "provider unavailable", False, None)])
        claude = ScriptedAdapter([self.ai_done(path)])

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=claude), 0)

        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(claude.calls), 1)
        failover = next(entry for entry in read_entries(
            journal_path_for(path)) if entry.get("event") == "adapter_failover")
        self.assertEqual(failover["agent"], "codex")
        self.assertIn("codex -> claude", failover["summary"])

    def test_task_role_authentication_failure_fails_over_without_retry(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", '
              'adapter = ["codex", "claude"] }]\n'))
        self.commit_all()
        codex = ScriptedAdapter([TaskResult(
            1, "Not logged in", False, None,
            failure_kind="authentication")])
        claude = ScriptedAdapter([self.ai_done(path)])

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=claude), 0)

        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_task_role_all_authentication_failures_stop_resumable(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", '
              'adapter = ["codex", "claude"] }]\n'))
        self.commit_all()
        authentication = TaskResult(
            1, "Not logged in", False, None,
            failure_kind="authentication")
        codex = ScriptedAdapter([authentication])
        claude = ScriptedAdapter([authentication])
        sleeps: list[float] = []

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append), 1)

        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(parse_task_file(path).status, "WIP")
        self.assertIn("AUTHENTICATION REQUIRED", output.getvalue())

    def test_task_role_authentication_then_quota_waits_only_for_quota(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", '
              'adapter = ["codex", "claude"] }]\n'))
        cfg.rotation_poll_minutes = 2
        self.commit_all()
        codex = ScriptedAdapter([TaskResult(
            1, "Not logged in", False, None,
            failure_kind="authentication")])
        claude = ScriptedAdapter([
            TaskResult(1, "quota", True, None), self.ai_done(path)])
        sleeps: list[float] = []

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=claude, sleep=sleeps.append), 0)

        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(sum(sleeps), 120)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_task_role_waits_after_every_declared_adapter_is_unavailable(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", '
              'adapter = ["codex", "claude"] }]\n'))
        cfg.rotation_poll_minutes = 2
        self.commit_all()
        unavailable = TaskResult(1, "provider unavailable", False, None)
        codex = ScriptedAdapter([unavailable, self.ai_done(
            path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        claude = ScriptedAdapter([unavailable])
        sleeps: list[float] = []

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=claude, sleep=sleeps.append), 0)

        self.assertEqual(sum(sleeps), 120)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(len(claude.calls), 1)

    def test_task_role_combines_quota_and_unavailable_before_waiting(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = [{ role = "worker", '
              'adapter = ["codex", "claude"] }]\n'))
        cfg.rotation_poll_minutes = 2
        self.commit_all()
        codex = ScriptedAdapter([
            TaskResult(1, "", True, None),
            self.ai_done(path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        claude = ScriptedAdapter([
            TaskResult(1, "provider unavailable", False, None)])
        sleeps: list[float] = []

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(engine.run(
                cfg, once=True, adapter=claude, sleep=sleeps.append), 0)

        self.assertEqual(sum(sleeps), 120)
        self.assertEqual(len(codex.calls), 2)
        self.assertEqual(len(claude.calls), 1)

    def test_task_workflow_overrides_project_task_roles(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ("implementer",))
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()

        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("scheduled role: implementer", adapter.calls[0][0])
        self.assertNotIn("scheduled role: preparer", adapter.calls[0][0])

    def test_empty_task_workflow_uses_only_its_plan_unit(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ())
        cfg = self.build(extra_config=self.PLAN_ROLE)
        self.commit_all()

        result = ok_result()
        result.usage = (TokenUsage(output_tokens=9),)
        adapter = ScriptedAdapter([result])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertIn("Plan workflow step 1 of 1", adapter.calls[0][0])
        self.assertNotIn("Task workflow step", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")
        records, invalid = usage.read_records(cfg.assent_dir)
        plan_records = [record for record in records
                        if record["context"]["kind"] == "plan"]
        self.assertEqual(invalid, 0)
        self.assertEqual(len(plan_records), 1)
        self.assertEqual(plan_records[0]["models"][0]["output_tokens"], 9)

    def test_task_workflow_missing_role_refuses_before_any_session(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ("missing",))
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()
        adapter = ScriptedAdapter([])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = engine.run(cfg, once=True, adapter=adapter)

        self.assertEqual(result, 1)
        self.assertEqual(adapter.calls, [])
        self.assertIn("Task t001", out.getvalue())
        self.assertIn("missing agent role 'missing'", out.getvalue())

    def test_task_session_cannot_change_its_workflow(self):
        path = self.write_task(1)
        self.set_task_workflow(path, ("implementer",))
        cfg = self.build(retry=0, extra_config=self.TASK_ROLES)
        self.commit_all()

        def tamper(_prompt):
            text = path.read_text(encoding="utf-8")
            path.write_text(
                text.replace('{ role = "implementer" }',
                             '{ role = "preparer" }'),
                encoding="utf-8", newline="\n")
            set_status(path, "DONE")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                event="done", summary="Attempted closeout")
            return ok_result()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(
                engine.run(cfg, once=True, adapter=ScriptedAdapter([tamper])), 0)

        self.assertIn("fields other than status were modified: workflow",
                      out.getvalue())
        self.assertEqual(parse_task_file(path).status, "BLOCKED")

    def test_never_started_next_task_step_has_no_interrupt_prompt(self):
        path = self.write_task(1)
        cfg = self.build(extra_config=self.TASK_ROLES)
        self.commit_all()

        def prepare(_prompt):
            append_entry(journal_path_for(path), by="claude",
                         requested_model="lite", event="progress",
                         summary="Preparation completed")
            return ok_result()

        real_write = engine.write_workflow_state
        interrupted = False

        def stop_after_advance(tasks_dir, state):
            nonlocal interrupted
            real_write(tasks_dir, state)
            if state.step_index == 1 and not state.started and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with mock.patch.object(
                engine, "write_workflow_state", side_effect=stop_after_advance):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([prepare])), 130)

        resumed = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=resumed), 0)
        self.assertNotIn("previous adapter session was interrupted",
                         resumed.calls[0][0])

    def test_interrupted_blocked_task_workflow_resumes_instead_of_settling(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT + self.VERDICT_AGENT
            + '[workflow]\ntask = [{ role = "worker" }, '
              '{ action = "focused_test" }, { role = "task_repair" }, '
              '{ action = "focused_test" }]\n'
              'plan = []\nintegration = []\n'))
        self.commit_all()

        def blocked_then_interrupted(_prompt):
            set_status(path, "BLOCKED")
            append_entry(
                journal_path_for(path), by="claude",
                requested_model="lite", requested_effort="medium",
                event="blocked", summary="Task-local blocker remains")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True,
            adapter=ScriptedAdapter([blocked_then_interrupted])), 130)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        interrupted = read_workflow_state(cfg.tasks_dir)
        self.assertIsNotNone(interrupted)
        self.assertTrue(interrupted.started)

        resumed = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=resumed), 0)

        self.assertEqual(len(resumed.calls), 1)
        self.assertIn("previous adapter session was interrupted",
                      resumed.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_empty_task_workflow_runs_one_plan_unit(self):
        first = self.write_task(1, scope=("src/a.txt",))
        second = self.write_task(
            2, deps=("t001",), scope=("src/b.txt",))
        cfg = self.build(extra_config=self.PLAN_ROLE)
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("old\n", encoding="utf-8")
        (self.root / "src" / "b.txt").write_text("old\n", encoding="utf-8")
        self.commit_all()

        def implement(prompt):
            self.assertIn(str(first), prompt)
            self.assertIn(str(second), prompt)
            self.assertIn("t002: TODO; deps: t001", prompt)
            self.assertNotIn("Cumulative checkpoint diff", prompt)
            for name in ("a.txt", "b.txt"):
                target = self.execution_root() / "src" / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("done\n", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([implement])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(
            [task.status for task in Plan.parse(cfg.tasks_dir).tasks],
            ["DONE", "DONE"])
        for path in (first, second):
            entry = read_entries(journal_path_for(path))[-1]
            self.assertEqual(entry["by"], "scheduler")
            self.assertIn("plan_worker", entry["summary"])
        self.assertTrue(self.subjects()[0].startswith(
            "auto(plan01): workflow plan step plan_worker"))
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_plan_role_authentication_failure_fails_over_in_declared_order(self):
        task = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.PLAN_ROLE.replace(
                '{ role = "plan_worker" }',
                '{ role = "plan_worker", adapter = ["codex", "claude"] }')))
        self.commit_all()
        codex = ScriptedAdapter([TaskResult(
            1, "Not logged in", False, None,
            failure_kind="authentication")])
        claude = ScriptedAdapter([ok_result()])

        with mock.patch.object(engine, "get_adapter", return_value=codex):
            self.assertEqual(self.run_quiet(cfg, adapter=claude), 0)

        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(parse_task_file(task).status, "DONE")

    def test_non_verdict_plan_unit_tolerates_auto_fix_flag(self):
        task = self.write_task(1)
        cfg = self.build(extra_config=self.PLAN_ROLE)
        self.commit_all()

        adapter = ScriptedAdapter([ok_result()])
        self.assertEqual(self.run_quiet(
            cfg, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(parse_task_file(task).status, "DONE")

    def test_plan_unit_preflights_explicit_adapter_outside_rotation(self):
        task = self.write_task(1)
        self.set_task_workflow(task, ())
        cfg = self.build(extra_config=(
            '\n[abilities.review_plan]\n'
            'prompt = "Review the whole plan."\n'
            'writes = true\nproduces_verdict = true\n'
            '[roles.plan_reviewer]\nability = ["review_plan"]\n'
            'model = "lite"\neffort = "normal"\n'
            '[workflow]\ntask = []\n'
            'plan = [{ role = "plan_reviewer", adapter = "codex" }]\n'))
        self.commit_all()
        worker = ScriptedAdapter([])
        explicit = ScriptedAdapter([ok_result()])
        explicit.preflight = mock.Mock(return_value=["unsupported identity"])

        with mock.patch.object(engine, "get_adapter", return_value=explicit):
            self.assertEqual(self.run_quiet(cfg, adapter=worker), 1)

        explicit.preflight.assert_called_once()
        self.assertEqual(explicit.calls, [])

    def test_plan_gate_failure_retries_step_then_blocks_every_task(self):
        failing = 'python -c "raise SystemExit(3)"'
        first = self.write_task(1, verify=failing)
        second = self.write_task(2, deps=("t001",), verify=failing)
        cfg = self.build(retry=1, extra_config=self.PLAN_ROLE)
        self.commit_all()
        adapter = ScriptedAdapter([ok_result(), ok_result()])

        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(
            [task.status for task in Plan.parse(cfg.tasks_dir).tasks],
            ["BLOCKED", "BLOCKED"])
        for path in (first, second):
            entry = read_entries(journal_path_for(path))[-1]
            self.assertIn("plan_worker", entry["summary"])
            self.assertIn("exit code is non-zero", entry["detail"])
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_plan_unit_ports_primary_tree_escape_and_journals_every_task(self):
        first = self.write_task(1, scope=("src/a.txt",))
        second = self.write_task(2, deps=("t001",), scope=("src/b.txt",))
        (self.root / "src").mkdir()
        (self.root / "src" / "a.txt").write_text("old\n", encoding="utf-8")
        (self.root / "src" / "b.txt").write_text("old\n", encoding="utf-8")
        cfg = self.build(retry=1, extra_config=self.PLAN_ROLE)
        self.commit_all()

        def escape_into_primary(_prompt):
            (self.root / "src" / "a.txt").write_text(
                "escaped\n", encoding="utf-8")
            return ok_result()

        adapter = ScriptedAdapter([escape_into_primary, ok_result()])
        self.assertEqual(self.run_quiet(cfg, adapter=adapter), 0)

        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(
            (self.root / "src" / "a.txt").read_text(encoding="utf-8"), "old\n")
        self.assertEqual(
            (self.execution_root() / "src" / "a.txt").read_text(encoding="utf-8"),
            "escaped\n")
        for path in (first, second):
            escapes = [entry for entry in read_entries(journal_path_for(path))
                       if entry.get("event") == "main_tree_escape"]
            self.assertEqual(len(escapes), 1)
            self.assertIn("src/a.txt", escapes[0]["detail"])

    def test_plan_unit_retains_out_of_scope_primary_tree_escape(self):
        task = self.write_task(1, scope=("src/",))
        cfg = self.build(retry=0, extra_config=self.PLAN_ROLE)
        self.commit_all()

        def escape_outside_plan(_prompt):
            (self.root / "outside.txt").write_text("stray\n", encoding="utf-8")
            return ok_result()

        self.assertEqual(self.run_quiet(
            cfg, adapter=ScriptedAdapter([escape_outside_plan])), 0)

        self.assertEqual(parse_task_file(task).status, "BLOCKED")
        self.assertEqual(
            (self.root / "outside.txt").read_text(encoding="utf-8"), "stray\n")
        self.assertFalse((self.execution_root() / "outside.txt").exists())
        entries = read_entries(journal_path_for(task))
        self.assertFalse(any(entry.get("event") == "main_tree_escape"
                             for entry in entries))
        blocked = next(entry for entry in entries
                       if entry.get("event") == "blocked")
        self.assertIn("outside the plan's scope union", blocked["detail"])

    def test_focused_test_action_closes_task_without_duplicate_focused_run(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT + self.VERDICT_AGENT
            + '[workflow]\ntask = [{ role = "worker" }, '
              '{ action = "focused_test" }, { role = "task_repair" }, '
              '{ action = "focused_test" }]\n'))
        self.commit_all()
        passed = subprocess.CompletedProcess([], 0, "focused pass\n", "")

        def finish(prompt):
            self.assertIn("scheduler-owned focused_test action", prompt)
            self.assertNotIn("To verify yourself", prompt)
            return self.ai_done(path)(prompt)

        worker = ScriptedAdapter([finish])
        with mock.patch.object(
                engine, "_verify_subprocess", return_value=passed) as focused:
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=worker), 0)

        focused.assert_called_once()
        self.assertEqual(len(worker.calls), 1)
        self.assertEqual(focused.call_args.args[1], parse_task_file(path).verify)
        self.assertEqual(parse_task_file(path).status, "DONE")
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_worker_blocked_scope_omission_is_repaired_inside_task_workflow(self):
        path = self.write_task(1, scope=("src/base.py",))
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT + self.VERDICT_AGENT
            + '[workflow]\ntask = [{ role = "worker" }, '
              '{ action = "focused_test" }, { role = "task_repair" }, '
              '{ action = "focused_test" }]\n'))
        self.commit_all()

        def blocked(_prompt):
            set_status(path, "BLOCKED")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                requested_effort="medium", event="blocked",
                summary="Task scope omitted src/needed.py",
                detail="The required implementation cannot be written safely.")
            return ok_result()

        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py", "Required source path was omitted",
            "The worker BLOCKED evidence names src/needed.py.",
            kind="scope_amendment",
            recommendation="Repair and append this exact source path.",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "new_file"))

        def repair(prompt):
            self.assertIn("PRIOR TASK ROLE BLOCKED EVIDENCE", prompt)
            self.assertIn("Task scope omitted src/needed.py", prompt)
            self.assertNotIn("FOCUSED TEST EVIDENCE", prompt)
            (self.execution_root() / "src" / "needed.py").write_text(
                "value = 2\n", encoding="utf-8")
            set_status(path, "WIP")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                requested_effort="medium", event="fixed",
                summary="Repaired the omitted task scope path")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("FIXED", (finding,))), False, None)

        adapter = ScriptedAdapter([blocked, repair])
        passed = subprocess.CompletedProcess([], 0, "focused pass\n", "")
        with mock.patch.object(
                engine, "_verify_subprocess", return_value=passed) as focused:
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=adapter), 0)

        focused.assert_called_once()
        self.assertEqual(len(adapter.calls), 2)
        task = parse_task_file(path)
        self.assertEqual(task.status, "DONE")
        self.assertEqual(task.scope, ["src/base.py", "src/needed.py"])
        self.assertFalse(workflow_state_path(cfg.tasks_dir).exists())

    def test_read_only_task_verdict_pass_skips_its_optional_fixer(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[abilities.task_review]\n'
              'prompt = "Review the task blocker."\n'
              'writes = false\nproduces_verdict = true\n'
              '[abilities.task_fix]\nprompt = "Fix the task blocker."\n'
              'writes = true\n'
              '[roles.task_reviewer]\nability = ["task_review"]\n'
              '[roles.task_fixer]\nability = ["task_fix"]\n'
              '[workflow]\ntask = [{ role = "worker" }, '
              '{ action = "focused_test" }, { role = "task_reviewer" }, '
              '{ role = "task_fixer" }, { action = "focused_test" }]\n'))
        self.commit_all()

        def blocked(_prompt):
            set_status(path, "BLOCKED")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                requested_effort="medium", event="blocked",
                summary="Worker reported a blocker")
            return ok_result()

        def review(prompt):
            self.assertIn("PRIOR TASK ROLE BLOCKED EVIDENCE", prompt)
            self.assertIn("This is a read-only decision session", prompt)
            set_status(path, "WIP")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                requested_effort="medium", event="reviewed",
                summary="No repair is required")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("PASS", ())), False, None)

        adapter = ScriptedAdapter([blocked, review])
        passed = subprocess.CompletedProcess([], 0, "focused pass\n", "")
        with mock.patch.object(
                engine, "_verify_subprocess", return_value=passed) as focused:
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=adapter), 0)

        focused.assert_called_once()
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_failed_focused_test_reaches_role_and_reruns_after_source_change(self):
        path = self.write_task(1)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT + self.VERDICT_AGENT
            + '[workflow]\ntask = [{ action = "focused_test" }, '
              '{ role = "task_repair" }, { action = "focused_test" }]\n'))
        self.commit_all()

        def repair(prompt):
            self.assertIn("FOCUSED TEST EVIDENCE", prompt)
            self.assertIn("Status: FAILED", prompt)
            self.ai_done(path, files={"src/fixed.py": "fixed\n"})(prompt)
            finding = auto_fix.ReviewFinding(
                "t001", "src/fixed.py", "Focused regression failed",
                "The failed command requires this source repair.")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("FIXED", (finding,))), False, None)

        results = [
            subprocess.CompletedProcess([], 3, "focused failure\n", ""),
            subprocess.CompletedProcess([], 0, "focused pass\n", ""),
        ]
        with mock.patch.object(
                engine, "_verify_subprocess", side_effect=results) as focused:
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([repair])), 0)

        self.assertEqual(focused.call_count, 2)
        self.assertEqual(parse_task_file(path).status, "DONE")

    def test_role_after_focused_test_requires_a_later_action(self):
        path = self.write_task(1)
        with self.assertRaisesRegex(
                AssentError, "must end with focused_test"):
            self.build(retry=0, extra_config=(
                self.ACTION_AGENT
                + '[workflow]\ntask = [{ action = "focused_test" }, '
                  '{ role = "worker" }]\n'))

    def test_task_verdict_repairs_exact_scope_addition_in_same_session(self):
        path = self.write_task(1, scope=("src/base.py",))
        source = self.root / "src"
        source.mkdir()
        (source / "base.py").write_text("base = 1\n", encoding="utf-8")
        cfg = self.build(retry=0, extra_config=(
            self.VERDICT_AGENT
            + '[workflow]\ntask = [{ action = "focused_test" }, '
              '{ role = "task_repair" }, { action = "focused_test" }]\n'))
        self.commit_all()

        finding = auto_fix.ReviewFinding(
            "t001", "src/needed.py", "Required source path was omitted",
            "The failed focused test requires an edit to src/needed.py.",
            kind="scope_amendment",
            recommendation="Repair and append this exact source path.",
            scope_addition=auto_fix.ScopeAddition(
                "src/needed.py", "new_file"))

        def repair(prompt):
            self.assertIn("repair every reported blocker now", prompt)
            (self.execution_root() / "src" / "needed.py").write_text(
                "value = 2\n", encoding="utf-8")
            set_status(path, "WIP")
            append_entry(
                journal_path_for(path), by="claude", requested_model="lite",
                requested_effort="medium", event="fixed",
                summary="Repaired the omitted exact scope path")
            return TaskResult(
                0, auto_fix.review_record_json(
                    auto_fix.ReviewRecord("FIXED", (finding,))), False, None)

        results = [
            subprocess.CompletedProcess([], 3, "focused failure\n", ""),
            subprocess.CompletedProcess([], 0, "focused pass\n", ""),
        ]
        adapter = ScriptedAdapter([repair])
        with mock.patch.object(
                engine, "_verify_subprocess", side_effect=results) as focused:
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=adapter), 0)

        self.assertEqual(focused.call_count, 2)
        self.assertEqual(len(adapter.calls), 1)
        task = parse_task_file(path)
        self.assertEqual(task.status, "DONE")
        self.assertEqual(task.scope, ["src/base.py", "src/needed.py"])
        self.assertEqual(
            sum(entry.get("event") == "auto_fix_scope_amendment"
                for entry in read_entries(journal_path_for(path))), 1)

    def test_plan_action_only_passes_and_failed_evidence_reaches_plan_role(self):
        action_only = self.write_task(1)
        cfg = self.build(extra_config=(
            '[workflow]\ntask = []\nplan = [{ action = "focused_sweep" }]\n'))
        self.commit_all()
        passed = subprocess.CompletedProcess([], 0, "plan pass\n", "")
        with mock.patch.object(
                engine, "_verify_subprocess",
                return_value=passed) as focused_sweep:
            self.assertEqual(self.run_quiet(
                cfg, adapter=ScriptedAdapter([])), 0)
        focused_sweep.assert_called_once()
        self.assertEqual(parse_task_file(action_only).status, "DONE")
        self.assertFalse((self.plan_dir / "_verification.toml").exists())

        second = self.write_task(2)
        cfg = self.build(retry=0, extra_config=(
            self.ACTION_AGENT
            + '[workflow]\ntask = []\nplan = [{ action = "focused_sweep" }, '
              '{ role = "worker" }, { action = "focused_sweep" }]\n'))

        def inspect(prompt):
            self.assertIn("FOCUSED SWEEP EVIDENCE", prompt)
            self.assertIn("Status: FAILED", prompt)
            self.assertIn("creates no verification receipt", prompt)
            target = self.execution_root() / "src" / "fixed.py"
            target.parent.mkdir(exist_ok=True)
            target.write_text("fixed\n", encoding="utf-8")
            return ok_result()

        failed = subprocess.CompletedProcess([], 5, "plan failure\n", "")
        passed = subprocess.CompletedProcess([], 0, "plan pass\n", "")
        with mock.patch.object(
                engine, "_verify_subprocess",
                side_effect=(failed, passed, passed)) as focused_sweep:
            self.assertEqual(self.run_quiet(
                cfg, adapter=ScriptedAdapter([inspect])), 0)
        self.assertEqual(focused_sweep.call_count, 3)
        self.assertEqual(parse_task_file(second).status, "DONE")


class TestAntigravitySession(GlobalContractsMixin, EngineTestCase):
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
        prompts: list[str] = []

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None,
                 input_text=None):
            commands.append(command)
            prompts.append(input_text)
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
        prompt = prompts[0]
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
        path = self.write_task(1, model="prime", effort="slight")
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter]\nname = "antigravity"\n', encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()
        t0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        sleeps: list[float] = []
        calls: list[list[str]] = []
        prompts: list[str] = []

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None,
                 input_text=None):
            calls.append(command)
            prompts.append(input_text)
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

        resume_prompt = prompts[1]
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


class TestAdapterProcessOutcomes(GlobalContractsMixin, EngineTestCase):
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


class TestBillingAbort(GlobalContractsMixin, EngineTestCase):
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


class TestQuotaAndResume(GlobalContractsMixin, EngineTestCase):
    def rotation_config(self):
        cfg = self.build()
        cfg.adapter_names = ("claude", "codex")
        return cfg

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
            # The adapter can write DONE before the process result is classified as quota;
            # the scheduler must put the task back into the resumable state first.
            set_status(path, "DONE")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=reset)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            return self.ai_done(path)(prompt)

        adapter = ScriptedAdapter([quota_step, resumed])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append, now=lambda: t0)
        self.assertEqual(rc, 0)
        self.assertAlmostEqual(sum(sleeps), (5 + 2) * 60, delta=1)  # +2 minute buffer
        self.assertIn("resume", adapter.calls[1][0])
        self.assertIn("Waiting for quota reset before resuming", out.getvalue())
        # one zero-token capability preflight before the run, then one per attempt
        self.assertEqual(adapter.resolve_calls, ["lite", "lite", "lite"])
        subjects = self.subjects()
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in subjects))
        self.assertEqual(
            len([s for s in subjects if s.startswith("auto(plan01/t001): ")]), 1)
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "lite")
        self.assertEqual(quota["requested_effort"], "medium")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, waiting for quota reset before resuming")
        self.assertNotIn("session", [e["event"] for e in entries])

    def test_unknown_quota_wait_names_poll_and_preserves_resume_progress(self):
        path = self.write_task(1)
        cfg = self.build()
        cfg.quota_poll_minutes = 7
        self.commit_all()

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            set_status(path, "DONE")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            self.assertIn("resume", prompt.lower())
            return self.ai_done(path)(prompt)

        adapter = ScriptedAdapter([quota_step, resumed])
        sleeps: list[float] = []
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg, once=True, adapter=adapter,
                            sleep=sleeps.append)

        self.assertEqual(rc, 0)
        self.assertEqual(sum(sleeps), 7 * 60)
        self.assertEqual(parse_task_file(path).status, "DONE")
        terminal = out.getvalue()
        self.assertIn(
            "Waiting for quota poll (every 7 minutes) before resuming", terminal)
        self.assertIn("Quota poll (every 7 minutes)", terminal)
        self.assertNotIn("reset", terminal.lower())
        self.assertIn("src/partial.py", self._git_execution("ls-files"))
        self.assertTrue(any(s.startswith("wip(plan01/t001): ")
                            for s in self.subjects()))

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(e for e in entries if e["event"] == "quota")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, waiting for quota poll "
            "(every 7 minutes) before resuming")
        self.assertNotIn("reset", quota["summary"].lower())

    def test_checkpoint_resume_keeps_same_adapter_without_wait_rotation_or_retry(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        cfg.adapter_names = ("claude", "codex")
        self.commit_all()
        control = TaskResult(
            exit_code=1, output=CHECKPOINT_RESUME_RECORD + "\n",
            quota_exhausted=False, reset_at=None, checkpoint_resume=True)

        def checkpoint_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            set_status(path, "DONE")
            return control

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "WIP")
            return self.ai_done(path)(prompt)

        claude = ScriptedAdapter(
            [checkpoint_step, resumed], resolved_model="claude-lite")
        codex = ScriptedAdapter([], resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            with mock.patch("assent.engine.get_adapter", return_value=codex):
                result = engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append)

        self.assertEqual(result, 0)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(codex.calls, [])
        self.assertEqual(sleeps, [])
        self.assertEqual(claude.calls[0][1:], claude.calls[1][1:])
        self.assertIn("resume", claude.calls[1][0].lower())

        terminal = out.getvalue()
        self.assertIn("Checkpoint-resume control received", terminal)
        self.assertIn("same adapter command", terminal)
        self.assertNotIn("Waiting for quota reset", terminal)
        self.assertNotIn("Switching adapter", terminal)

        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        checkpoint = next(entry for entry in entries
                          if entry["event"] == "checkpoint_resume")
        self.assertEqual(checkpoint["agent"], "claude")
        self.assertEqual(checkpoint["requested_model"], "claude-lite")
        self.assertEqual(checkpoint["requested_effort"], "medium")
        self.assertIn(CHECKPOINT_RESUME_RECORD, checkpoint["detail"])
        self.assertNotIn("quota", [entry["event"] for entry in entries])
        self.assertTrue(any(subject.startswith("wip(plan01/t001): ")
                            for subject in self.subjects()))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_quota_preserves_an_explicit_blocked_result(self):
        path = self.write_task(1)
        cfg = self.build(retry=0)
        self.commit_all()

        def blocked_quota(prompt):
            set_status(path, "BLOCKED")
            return TaskResult(exit_code=1, output="", quota_exhausted=True,
                              reset_at=None)

        def resumed(prompt):
            self.assertEqual(parse_task_file(path).status, "BLOCKED")
            return ok_result()

        self.assertEqual(
            engine.run(cfg, once=True, sleep=lambda _: None,
                       adapter=ScriptedAdapter([blocked_quota, resumed])), 0)
        self.assertEqual(parse_task_file(path).status, "BLOCKED")
        self.assertFalse(any(s.startswith("auto(plan01/t001): ")
                             for s in self.subjects()))

    def test_quota_rotates_to_next_adapter_and_records_each_identity(self):
        path = self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()

        def quota_step(prompt):
            root = self.execution_root()
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "partial.py").write_text("kept", encoding="utf-8")
            return TaskResult(
                exit_code=1, output="", quota_exhausted=True, reset_at=None)

        claude = ScriptedAdapter([quota_step], resolved_model="claude-lite")
        codex = ScriptedAdapter(
            [self.ai_done(
                path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with contextlib.redirect_stdout(out):
            with mock.patch("assent.engine.get_adapter", return_value=codex):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append), 0)

        self.assertEqual(sleeps, [])
        self.assertIn("resume", codex.calls[0][0])
        terminal = out.getvalue()
        self.assertIn("Switching adapter claude -> codex immediately", terminal)
        self.assertNotIn("waiting for reset", terminal.lower())
        self.assertNotIn("rotation poll", terminal.lower())
        self.assertTrue(any(
            subject.startswith("wip(plan01/t001): ")
            for subject in self.subjects()))
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        quota = next(entry for entry in entries if entry["event"] == "quota")
        self.assertEqual(quota["agent"], "claude")
        self.assertEqual(quota["requested_model"], "claude-lite")
        self.assertEqual(
            quota["summary"],
            "Quota exhausted; progress kept, switching immediately to adapter codex")
        self.assertNotIn("wait", quota["summary"].lower())
        done = next(entry for entry in entries if entry["by"] == "codex")
        self.assertEqual(done["requested_model"], "codex-lite")

    def test_rotation_resolves_each_adapter_effort_and_names_it_in_the_line(self):
        # Each adapter resolves its own built-in lite default independently
        # (claude normal -> medium, codex slight -> low), and the opening line says
        # which one is running.
        path = self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()

        def quota_step(prompt):
            return TaskResult(
                exit_code=1, output="", quota_exhausted=True, reset_at=None)

        claude = ScriptedAdapter([quota_step], resolved_model="claude-lite")
        codex = ScriptedAdapter(
            [self.ai_done(path, by="codex", requested_model="codex-lite")],
            resolved_model="codex-lite")
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                self.assertEqual(engine.run(
                    cfg, once=True, adapter=claude, sleep=lambda _: None), 0)

        self.assertEqual(claude.calls[0][2], "medium")
        self.assertEqual(codex.calls[0][2], "low")
        self.assertEqual(
            [line for line in out.getvalue().splitlines() if "Session:" in line],
            ["  Session: claude | lite->claude-lite | normal->medium",
             "  Session: codex | lite->codex-lite | slight->low"])

    def test_complete_quota_rotation_waits_then_continues_from_next_adapter(self):
        path = self.write_task(1)
        cfg = self.rotation_config()
        cfg.rotation_poll_minutes = 2
        self.commit_all()
        quota = TaskResult(
            exit_code=1, output="", quota_exhausted=True, reset_at=None)
        claude = ScriptedAdapter(
            [quota, self.ai_done(path)], resolved_model="claude-lite")
        codex = ScriptedAdapter([quota], resolved_model="codex-lite")
        sleeps: list[float] = []
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                result = engine.run(
                    cfg, once=True, adapter=claude, sleep=sleeps.append)

        self.assertEqual(result, 0)
        self.assertEqual(sum(sleeps), 2 * 60)
        self.assertEqual(len(claude.calls), 2)
        self.assertEqual(len(codex.calls), 1)
        self.assertIn("Every adapter in the rotation is quota-exhausted",
                      out.getvalue())
        self.assertIn("continuing with claude", out.getvalue())
        self.assertEqual(out.getvalue().count(
            "Every adapter in the rotation is quota-exhausted"), 1)

        from assent.plan import read_entries
        quotas = [entry for entry in read_entries(journal_path_for(path))
                  if entry["event"] == "quota"]
        self.assertEqual(len(quotas), 2)
        self.assertEqual(
            [(entry["agent"], entry["requested_model"])
             for entry in quotas],
            [("claude", "claude-lite"), ("codex", "codex-lite")])
        self.assertEqual(
            quotas[0]["summary"],
            "Quota exhausted; progress kept, switching immediately to adapter codex")
        self.assertEqual(
            quotas[1]["summary"],
            "Quota exhausted; progress kept, every adapter in the rotation is "
            "quota-exhausted; waiting for rotation poll before continuing with claude")

    def test_all_rotation_adapters_are_preflighted_before_worktree_creation(self):
        self.write_task(1)
        cfg = self.rotation_config()
        self.commit_all()
        claude = ScriptedAdapter([ok_result()])
        codex = ScriptedAdapter([])
        codex.preflight = mock.Mock(return_value=["unsupported invocation"])
        out = io.StringIO()

        with mock.patch("assent.engine.get_adapter", return_value=codex):
            with contextlib.redirect_stdout(out):
                result = engine.run(cfg, once=True, adapter=claude)

        self.assertEqual(result, 1)
        self.assertEqual(claude.calls, [])
        self.assertEqual(codex.calls, [])
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())
        codex.preflight.assert_called_once()
        self.assertIn("codex capability preflight: FAIL", out.getvalue())

    def test_wip_task_resumed_on_startup(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(path)])
        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertIn("resume", adapter.calls[0][0])
        self.assertEqual(parse_task_file(path).status, "DONE")


class TestInterruptedTaskResume(GlobalContractsMixin, EngineTestCase):
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

    def test_interrupt_during_post_auto_report_keeps_done_without_duplicate_auto(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        refreshes = 0

        def interrupt_first_report(cfg):
            nonlocal refreshes
            refreshes += 1
            if refreshes == 1:
                raise KeyboardInterrupt

        with mock.patch.object(engine, "try_write_report",
                               side_effect=interrupt_first_report):
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=ScriptedAdapter([
                    self.ai_done(path, {"src/done.py": "done"})])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        autos = [s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]
        self.assertEqual(len(autos), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        # A later run sees the terminal task as already closed and cannot synthesize another
        # auto marker from the report-refresh interruption.
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_interrupt_after_dirty_terminal_auto_commit_keeps_done(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        real_commit_if_dirty = engine.gitops.commit_if_dirty

        def commit_then_interrupt(root, message, excludes=()):
            committed = real_commit_if_dirty(root, message, excludes)
            if message.startswith("auto(plan01/t001): "):
                raise KeyboardInterrupt
            return committed

        with mock.patch.object(engine.gitops, "commit_if_dirty",
                               side_effect=commit_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True,
                adapter=ScriptedAdapter([
                    self.ai_done(path, {"src/done.py": "done"})])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(entry["event"] == "interrupt" for entry in entries))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_interrupt_after_resumed_empty_terminal_auto_commit_keeps_done(self):
        path = self.write_task(1, status="WIP")
        cfg = self.build()
        self.commit_all()
        real_commit_empty = engine.gitops.commit_empty

        def empty_commit_then_interrupt(root, message):
            real_commit_empty(root, message)
            if message.startswith("auto(plan01/t001): "):
                raise KeyboardInterrupt

        with mock.patch.object(engine.gitops, "commit_empty",
                               side_effect=empty_commit_then_interrupt):
            self.assertEqual(self.run_quiet(
                cfg, once=True, adapter=ScriptedAdapter([
                    self.ai_done(path)])), 130)

        self.assertEqual(parse_task_file(path).status, "DONE")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertFalse(any(entry["event"] == "interrupt" for entry in entries))
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)
        self.assertNotIn("wip(plan01/t001): user interrupt", self.subjects())

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([])), 0)
        self.assertEqual(
            len([s for s in self.subjects()
                 if s.startswith("auto(plan01/t001): ")]), 1)

    def test_matching_auto_commit_before_terminal_closeout_cannot_recover_done(self):
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()
        expected = "auto(plan01/t001): task"

        def interrupted(prompt):
            root = self.execution_root()
            (root / "src" / "before_closeout.py").parent.mkdir(exist_ok=True)
            (root / "src" / "before_closeout.py").write_text(
                "work", encoding="utf-8")
            # This terminal-looking commit belongs to the adapter phase, before the scheduler
            # has passed _evaluate and armed its closeout witness.
            gitops.commit_empty(root, expected)
            set_status(path, "DONE")
            raise KeyboardInterrupt

        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([interrupted])), 130)

        self.assertEqual(parse_task_file(path).status, "WIP")
        from assent.plan import read_entries
        entries = read_entries(journal_path_for(path))
        self.assertTrue(any(entry["event"] == "interrupt" for entry in entries))
        self.assertIn(expected, self.subjects())
        self.assertTrue(any(
            subject.startswith("wip(plan01/t001): ")
            for subject in self.subjects()))

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

    def test_stop_wake_during_quota_wait_reaches_the_interrupt_cleanup(self):
        """The wake is only a wake: it releases the sleeping main thread so the
        stdin watcher's already-pending KeyboardInterrupt is delivered, and the
        ordinary interrupt cleanup then runs unchanged."""
        path = self.write_task(1)
        cfg = self.build()
        self.commit_all()

        quota = TaskResult(exit_code=1, output="", quota_exhausted=True,
                           reset_at=None)
        parked = threading.Event()

        def stop_the_run() -> None:
            """Exactly what the stdin stop watcher does on EOF, in that order:
            mark the interrupt, then release the wait it is stuck behind."""
            parked.wait(30)
            _thread.interrupt_main()
            wake_stop_waiters()

        waker = threading.Thread(target=stop_the_run, daemon=True)
        self.addCleanup(waker.join, 30)
        self.addCleanup(clear_stop_wake)

        def sleep(seconds):
            # The production wait, entered on a segment of the full length: a
            # pending KeyboardInterrupt alone would sit here for 60 seconds.
            self.assertEqual(seconds, engine._COUNTDOWN_SEGMENT)
            parked.set()
            interruptible_sleep(seconds)

        waker.start()
        started = time.monotonic()
        self.assertEqual(self.run_quiet(
            cfg, once=True, adapter=ScriptedAdapter([quota]),
            sleep=sleep), 130)
        self.assertLess(time.monotonic() - started, engine._COUNTDOWN_SEGMENT)

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


class TestQuotaMath(GlobalContractsMixin, EngineTestCase):
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

    def test_countdown_non_tty_single_line_in_bounded_segments(self):
        stream = io.StringIO()  # isatty() False
        sleeps: list[float] = []
        engine._countdown(150, "Quota reset", sleeps.append, stream=stream)
        # One message, but never one long sleep: the total is unchanged and no
        # single segment exceeds the constant.
        self.assertEqual(sum(sleeps), 150)
        self.assertLessEqual(max(sleeps), engine._COUNTDOWN_SEGMENT)
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertNotIn("\r", stream.getvalue())

    def test_countdown_non_tty_stop_lands_within_one_segment(self):
        """A stop request reaches a multi-hour quota wait promptly.

        The stdin stop channel calls _thread.interrupt_main(); on POSIX that
        only makes the exception pending until bytecode next runs, which is
        the end of a segment. The injected sleep stands in for that delivery.
        """
        stream = io.StringIO()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            engine._countdown(10405, "Quota reset", sleep, segment=0.5,
                              stream=stream)
        self.assertEqual(sleeps, [0.5])

    def test_countdown_stops_counting_down_once_a_stop_is_requested(self):
        """A woken segment must not be followed by the rest of a multi-hour
        wait; the pending KeyboardInterrupt lands at the next bytecode."""
        self.addCleanup(clear_stop_wake)
        stream = io.StringIO()
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            wake_stop_waiters()   # what the stdin watcher does mid-wait

        engine._countdown(10405, "Quota reset", sleep, stream=stream)
        self.assertEqual(sleeps, [engine._COUNTDOWN_SEGMENT])

    def test_stale_stop_request_does_not_shorten_a_later_countdown(self):
        """`run` is also a library and test entry point, so one stop request
        must not make every later countdown return immediately."""
        self.addCleanup(clear_stop_wake)
        wake_stop_waiters()
        stream = io.StringIO()
        sleeps: list[float] = []
        engine._countdown(150, "Quota reset", sleeps.append, stream=stream)
        self.assertEqual(sum(sleeps), 150)

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


if __name__ == "__main__":
    unittest.main()
