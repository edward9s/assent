"""inspection tests: the read-only report / status / check commands.

These commands take no lock, start no session and change no Git state, so each case checks
exactly what they print for a folder as it stands -- the progress counts and next task, the
environment and format verdicts, the rendered report with its checkpoint hashes, and the
stack lines both surfaces share. Runs appear here only as a way to produce the state being
reported on. Shared fixtures come from tests.engine_support.

Chinese literals that remain are deliberate user/upstream passthrough data (task titles,
journal summaries) used to prove that non-English data flows through verbatim."""
import contextlib
import io
import unittest
from unittest import mock

from assent import AssentError, auto_fix, contracts, gitops, inspection, preflight
from assent.config import load_config
from assent.plan import Plan, journal_path_for, set_status
from tests.engine_support import (_FAILV, EngineTestCase, ScriptedAdapter,
                                  ok_result, task_text)
from tests.test_contracts import GlobalContractsMixin


class TestQueries(GlobalContractsMixin, EngineTestCase):
    def test_status_reports_counts_and_next(self):
        self.write_task(1, status="DONE")
        self.write_task(2, deps=("t001",), title="第二個")
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.status(cfg), 0)
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
            self.assertEqual(inspection.check(cfg), 0)
        self.assertIn("Result: passed", out.getvalue())

    def test_check_reports_default_auto_fix_reviewer_identity_and_provenance(self):
        self.write_task(1, model="core")
        cfg = self.build()
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        text = out.getvalue()
        self.assertIn(
            "Auto-fix reviewer (resolved): claude / prime->fable / heavy->high",
            text)
        self.assertIn(
            "reviewer adapter = claude (source: project "
            "(explicit settings layer) (first effective worker adapter fallback))",
            text)
        self.assertIn(
            "reviewer model = prime -> fable (setting source: "
            "builtin (built-in fallback)", text)
        self.assertIn(
            "reviewer effort = heavy -> high (setting source: "
            "builtin (built-in fallback)", text)

    def test_check_reports_explicit_auto_fix_reviewer_settings(self):
        self.write_task(1, model="core")
        cfg = self.build(extra_config=(
            "[auto_fix.review]\n"
            'model = "core"\n'
            'effort = "slight"\n'))
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        text = out.getvalue()
        self.assertIn(
            "Auto-fix reviewer (resolved): claude / core->opus / slight->low",
            text)
        self.assertIn(
            "reviewer model = core -> opus (setting source: project "
            "(explicit settings layer)", text)
        self.assertIn(
            "reviewer effort = slight -> low (setting source: project "
            "(explicit settings layer)", text)

    def test_check_fails_on_dependency_cycle(self):
        self.write_task(1, deps=("t002",))
        self.write_task(2, deps=("t001",))
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 1)
        self.assertIn("FAIL", out.getvalue())

    def test_check_validates_selected_folder_declaration(self):
        self.write_task(1)
        (self.plan_dir / "_folder.toml").write_text(
            'after = []\nunknown = true\n', encoding="utf-8")
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 1)
        self.assertIn("Folder dependencies: FAIL", out.getvalue())
        self.assertIn("unknown keys", out.getvalue())

    def test_check_displays_resolved_assignment_and_default_marker(self):
        self.write_task(1, slug="任務分配顯示", model="core")
        self.write_task(2, slug="explicit", model="lite", effort="heavy",
                        status="DONE")
        cfg = self.build(adapter_name="codex", extra_config=(
            '[adapter.codex]\ncommand = "python"\n'
            '[adapter.codex.models]\n'
            'core = "gpt-5.6-luna"\n'
            'lite = "gpt-lite"\n'
            '[adapter.codex.default_effort]\n'
            'core = "heavy"\n'
            'lite = "slight"\n'
            '[adapter.codex.efforts.core]\n'
            'heavy = "max"\n'
            '[adapter.codex.efforts.lite]\n'
            'heavy = "max"\n'
            '[auto_fix.review]\nmodel = "core"\n'))
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        text = out.getvalue()
        self.assertIn("Task assignment (adapter = codex):", text)
        self.assertIn("t001_任務分配顯示", text)
        self.assertIn("core/heavy*", text)
        self.assertIn("gpt-5.6-luna/max", text)
        self.assertIn("lite/heavy", text)
        self.assertIn("gpt-lite/max", text)
        self.assertIn("(* effort filled from default_effort)", text)

    def test_check_shows_the_builtin_effort_when_the_table_is_empty(self):
        # An empty default_effort table leaves the built-in codex core default (normal)
        # in place, so the assignment still names both the abstract and the actual value.
        self.write_task(1, model="core")
        cfg = self.build(adapter_name="codex", extra_config=(
            '[adapter.codex]\ncommand = "python"\n'
            '[adapter.codex.models]\ncore = "gpt-core"\n'
            '[adapter.codex.default_effort]\n'
            '[auto_fix.review]\nmodel = "core"\n'))
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        line = next(line for line in out.getvalue().splitlines()
                    if "t001_task" in line)
        self.assertRegex(line, r"core/normal\*\s+-> gpt-core/medium$")
        self.assertIn("(* effort filled from default_effort)", out.getvalue())

    def test_check_truncates_cjk_task_names_without_exceeding_line_width(self):
        self.write_task(1, slug="這是一個非常非常長的任務名稱甲乙丙丁戊己庚辛壬癸",
                        model="core", effort="slight")
        cfg = self.build(adapter_name="codex", extra_config=(
            '[adapter.codex]\ncommand = "python"\n'))
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        line = next(line for line in out.getvalue().splitlines()
                    if line.startswith("  t001_"))
        self.assertEqual(line.count("…"), 1)
        self.assertLessEqual(preflight._display_width(line), 78)

    def test_check_displays_one_assignment_block_per_adapter(self):
        self.write_task(1, model="core", effort="slight")
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter]\nname = ["claude", "codex"]\n'
            '[adapter.claude]\ncommand = "python"\n'
            '[adapter.codex]\ncommand = "python"\n',
            encoding="utf-8")
        cfg = load_config(self.root / ".assent" / "assent.toml", "plan01")
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        text = out.getvalue()
        self.assertEqual(text.count("Task assignment (adapter = "), 2)
        self.assertIn("Task assignment (adapter = claude):", text)
        self.assertIn("Task assignment (adapter = codex):", text)
        self.assertIn("opus/low", text)
        self.assertIn("gpt-5.6-terra/low", text)

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
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("t001  DONE", text)
        self.assertIn("t002  BLOCKED", text)
        self.assertIn("last journal (scheduler)", text)
        self.assertIn("[", text)  # DONE task carries a checkpoint hash
        # _report.md written out, but not version-controlled
        self.assertTrue((cfg.tasks_dir / "_report.md").is_file())
        self.assertNotIn("_report.md", self._git_execution("ls-files"))

    def _write_auto_fix_state(self, cfg, verdict="PASS"):
        plan = Plan.parse(cfg.tasks_dir)
        source_tree = gitops.tree_of(cfg.root, "HEAD")
        findings = () if verdict == "PASS" else (
            auto_fix.ReviewFinding(
                "t001", "src/main.py", "Blocking implementation issue",
                "The implementation does not satisfy the task contract."),)
        review = cfg.auto_fix_review
        reviewer_adapter = review.adapter if review is not None else "codex"
        reviewer_model = (review.requested_model
                          if review is not None else "gpt-5.6-sol")
        reviewer_effort = (review.requested_effort
                           if review is not None else "max")
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord(verdict, findings),
            source_tree=source_tree,
            task_plan_sha256=auto_fix.sha256_files(
                task.path for task in plan.tasks),
            review_prompt_sha256="3" * 64,
            reviewer_adapter=reviewer_adapter, reviewer_model=reviewer_model,
            reviewer_effort=reviewer_effort)
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg), state)

    def test_report_renders_fresh_auto_fix_pass_and_fail_evidence(self):
        self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()

        self._write_auto_fix_state(cfg, "PASS")
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("Folder auto-fix: PASSED (fresh)", text)
        self.assertIn("Source tree:", text)

        self._write_auto_fix_state(cfg, "FAIL")
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("Folder auto-fix: FAILED (fresh)", text)
        self.assertIn("Blocking implementation issue", text)

    def test_report_renders_missing_malformed_and_stale_auto_fix_evidence(self):
        self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        state_path = auto_fix.auto_fix_state_path(cfg)

        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("Folder auto-fix: NOT RUN", text)

        state_path.write_text("not valid toml = [\n", encoding="utf-8")
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("Folder auto-fix: STALE (malformed review state:", text)

        self._write_auto_fix_state(cfg, "PASS")
        (self.root / "source-change.py").write_text("changed\n", encoding="utf-8")
        self.commit_all("source change")
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("Folder auto-fix: STALE (source tree changed)", text)

    def test_report_marks_reviewer_configuration_drift_stale(self):
        self.write_task(1, status="DONE")
        cfg = self.build(extra_config=(
            '[auto_fix.review]\n'
            'adapter = "codex"\n'
            'model = "prime"\n'
            'effort = "heavy"\n'))
        self.commit_all()
        self._write_auto_fix_state(cfg, "PASS")

        drifted = self.build(extra_config=(
            '[auto_fix.review]\n'
            'adapter = "codex"\n'
            'model = "core"\n'
            'effort = "heavy"\n'))
        text = inspection.render_report(
            drifted, Plan.parse(drifted.tasks_dir))
        self.assertIn("Folder auto-fix: STALE (reviewer configuration changed)",
                      text)

    def test_report_renders_recovery_evidence_and_persistent_debt_agenda(self):
        task_path = self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        plan = Plan.parse(cfg.tasks_dir)
        source_tree = gitops.tree_of(cfg.root, "HEAD")
        task_digest = auto_fix.sha256_files(task.path for task in plan.tasks)
        finding = auto_fix.ReviewFinding(
            "t001", "assent/inspection.py", "Existing debt needs a follow-up",
            "The changed inspection path has no focused regression coverage.",
            kind="eligible_technical_debt",
            recommendation="Add a focused report regression before acceptance.")
        scope_finding = auto_fix.ReviewFinding(
            "t001", "tests/test_inspection.py", "Scope needs one exact addition",
            "The report test needs a declared existing path.",
            kind="scope_amendment",
            scope_addition=auto_fix.ScopeAddition(
                "tests/test_inspection.py", "existing_file"))
        state = auto_fix.state_for_review(
            auto_fix.ReviewRecord("FAIL", (finding, scope_finding)),
            source_tree=source_tree,
            task_plan_sha256=task_digest,
            review_prompt_sha256="4" * 64,
            reviewer_adapter=cfg.auto_fix_review.adapter,
            reviewer_model=cfg.auto_fix_review.requested_model,
            reviewer_effort=cfg.auto_fix_review.requested_effort)
        fingerprint = state.current_finding_fingerprints[0]
        state = auto_fix.with_worker_dispositions(
            state, (auto_fix.WorkerDisposition(
                "t001", fingerprint, "fixed", "Focused regression passes."),))
        state = auto_fix.with_repair_briefs(
            state, (auto_fix.RepairBrief(
                "t001", state.current_finding_fingerprints,
                "Original blocker evidence:\nThe old report omitted debt.\n\n"
                "Focused command evidence:\nThe focused test passes."),))
        state = auto_fix.consume_fixer_profile(
            state, auto_fix.FixerProfile("claude", "core", "heavy"))
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg), state)

        inspection.write_report(cfg, plan)
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        debt = (cfg.tasks_dir / "_technical_debt.md").read_text(encoding="utf-8")
        self.assertIn("Phase: NEEDS_REPAIR", report)
        self.assertIn("Original blocker evidence (t001): The old report omitted debt.",
                      report)
        self.assertIn("Current findings and recommendations:", report)
        self.assertIn("Add a focused report regression before acceptance.", report)
        self.assertIn("Approved scope additions:", report)
        self.assertIn("Repair acknowledgements:", report)
        self.assertIn("fixed; Focused regression passes.", report)
        self.assertIn("Consumed fixer profiles:", report)
        self.assertIn("claude/core/heavy", report)
        self.assertIn("TECHNICAL DEBT REVIEW REQUIRED", report)
        self.assertIn("Existing debt needs a follow-up", debt)
        self.assertIn("CURRENT / unresolved in the latest review", debt)
        self.assertIn("Add a focused report regression before acceptance.", debt)

        passed = auto_fix.state_for_review(
            auto_fix.ReviewRecord("PASS", ()),
            source_tree=source_tree,
            task_plan_sha256=task_digest,
            review_prompt_sha256="4" * 64,
            reviewer_adapter=cfg.auto_fix_review.adapter,
            reviewer_model=cfg.auto_fix_review.requested_model,
            reviewer_effort=cfg.auto_fix_review.requested_effort,
            previous=state, review_stage="recheck")
        auto_fix.write_auto_fix_state(
            auto_fix.auto_fix_state_path(cfg), passed)
        inspection.write_report(cfg, Plan.parse(cfg.tasks_dir))
        report = (cfg.tasks_dir / "_report.md").read_text(encoding="utf-8")
        debt = (cfg.tasks_dir / "_technical_debt.md").read_text(encoding="utf-8")
        self.assertIn("Folder auto-fix: PASSED (fresh)", report)
        self.assertIn("prior findings cleared", report)
        self.assertIn("TECHNICAL DEBT REVIEW REQUIRED", report)
        self.assertIn("RESOLVED / absent from the latest current findings", debt)

        blocked_debt = auto_fix.ReviewFinding(
            "t001", "assent/inspection.py", "Blocked debt", "blocked evidence",
            kind="eligible_technical_debt")
        with self.assertRaisesRegex(AssentError, "limited to initial completed-folder"):
            auto_fix.state_for_review(
                auto_fix.ReviewRecord("FAIL", (blocked_debt,)),
                source_tree=source_tree,
                task_plan_sha256=task_digest,
                review_prompt_sha256="4" * 64,
                reviewer_adapter=cfg.auto_fix_review.adapter,
                reviewer_model=cfg.auto_fix_review.requested_model,
                reviewer_effort=cfg.auto_fix_review.requested_effort,
                review_context="blocked_adjudication",
                failure_trigger="worker_blocked")

        recheck_debt = auto_fix.ReviewFinding(
            "t001", "assent/inspection.py", "New debt", "new evidence",
            kind="eligible_technical_debt", transition="newly_exposed",
            transition_evidence="t001 existing requirement exposed by repair")
        with self.assertRaisesRegex(AssentError, "limited to initial completed-folder"):
            auto_fix.state_for_review(
                auto_fix.ReviewRecord("FAIL", (recheck_debt,)),
                source_tree=source_tree,
                task_plan_sha256=task_digest,
                review_prompt_sha256="4" * 64,
                reviewer_adapter=cfg.auto_fix_review.adapter,
                reviewer_model=cfg.auto_fix_review.requested_model,
                reviewer_effort=cfg.auto_fix_review.requested_effort,
                previous=passed, review_stage="recheck")

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

        current = inspection.render_report(cfg, inspection.Plan.parse(cfg.tasks_dir))
        other = inspection.render_report(
            other_cfg, inspection.Plan.parse(other_cfg.tasks_dir))

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
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("last journal (ai): 舊日誌仍可讀", text)

    def test_try_write_report_does_not_swallow_process_control_exceptions(self):
        cfg = self.build()
        self.write_task(1)
        with mock.patch.object(inspection, "write_report", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                inspection.try_write_report(cfg)

class TestCheckContractsAndSources(GlobalContractsMixin, EngineTestCase):
    """`check` answers the two questions a layered setup makes ambiguous: are the
    global contracts current, and which layer decided each setting the printed task
    assignment actually used."""

    def write_user_config(self, text: str) -> None:
        (self.user_home / "assent.toml").write_text(text, encoding="utf-8")

    def load(self):
        # The project file is the locator whether or not it exists.
        return load_config(self.root / ".assent" / "assent.toml", "plan01")

    def check_output(self, cfg, expected_code=0) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), expected_code)
        return out.getvalue()

    def test_a_missing_contract_is_a_focused_check_failure(self):
        self.write_task(1)
        cfg = self.build()
        self.commit_all()
        (self.user_home / "format.md").unlink()

        text = self.check_output(cfg, expected_code=1)
        self.assertIn("Global contracts: FAIL", text)
        self.assertIn(f"{self.user_home / 'format.md'} is missing", text)
        self.assertIn(contracts.CONTRACT_REMEDY, text)
        # Focused: the contract that is still current is not named as a problem,
        # and the unrelated verdicts are still reported.
        self.assertNotIn(f"{self.user_home / 'instructions.md'} is", text)
        self.assertIn("Task-file format: OK", text)

    def test_current_contracts_pass_and_are_reported_once(self):
        self.write_task(1)
        cfg = self.build()
        self.commit_all()

        text = self.check_output(cfg)
        self.assertIn(f"Global contracts: OK ({self.user_home}: "
                      "instructions.md, format.md current)", text)
        self.assertIn("Result: passed", text)

    def test_the_user_config_alone_resolves_and_reports_its_assignments(self):
        self.write_task(1, model="core")
        self.write_user_config(
            '[adapter]\nname = "codex"\n'
            '[adapter.codex]\ncommand = "python"\n'
            '[adapter.codex.models]\ncore = "gpt-from-user"\n'
            '[auto_fix.review]\nmodel = "core"\n')
        cfg = self.load()
        self.commit_all()

        text = self.check_output(cfg)
        self.assertIn(f"Config sources (lowest priority first): builtin, "
                      f"user ({self.user_home / 'assent.toml'})", text)
        self.assertIn(f"  project override {self.root / '.assent' / 'assent.toml'}: "
                      "absent (optional)", text)
        self.assertIn("Task assignment (adapter = codex):", text)
        self.assertIn("gpt-from-user", text)
        self.assertIn("Setting sources: adapter.name = user (active: codex)", text)
        self.assertIn("codex: models.core = user", text)

    def test_a_project_override_reports_the_winning_project_keys(self):
        self.write_task(1, model="core")
        self.write_user_config(
            '[adapter]\nname = "codex"\n'
            '[adapter.codex]\ncommand = "python"\n'
            '[adapter.codex.models]\ncore = "gpt-from-user"\n'
            '[adapter.codex.default_effort]\ncore = "slight"\n'
            '[auto_fix.review]\nmodel = "core"\n')
        (self.root / ".assent" / "assent.toml").write_text(
            '[adapter.codex.models]\ncore = "gpt-from-project"\n',
            encoding="utf-8")
        cfg = self.load()
        self.commit_all()

        text = self.check_output(cfg)
        self.assertIn(
            f"user ({self.user_home / 'assent.toml'}), "
            f"project ({self.root / '.assent' / 'assent.toml'})", text)
        self.assertNotIn("absent (optional)", text)
        # The adapter name is still the user's; only the overridden model moved.
        self.assertIn("Setting sources: adapter.name = user (active: codex)", text)
        self.assertIn("codex: models.core = project", text)
        self.assertIn("default_effort.core = user", text)
        self.assertIn("gpt-from-project", text)

    def test_the_source_report_names_only_the_keys_the_assignment_used(self):
        self.write_task(1, model="lite")
        self.write_user_config(
            '[adapter]\nname = "claude"\n'
            '[adapter.claude]\ncommand = "python"\n'
            '[adapter.claude.models]\nlite = "haiku-from-user"\n'
            '[adapter.codex]\ncommand = "python"\n'
            '[auto_fix.review]\nmodel = "lite"\n'
            '[run]\nretry_per_task = 3\n')
        cfg = self.load()
        self.commit_all()

        text = self.check_output(cfg)
        source_line = next(line for line in text.splitlines()
                           if line.strip().startswith("claude:"))
        self.assertIn("models.lite = ", source_line)
        # No unrelated settings are dumped into the provenance report.
        self.assertNotIn("retry_per_task", text)
        self.assertNotIn("models.prime", source_line)
        self.assertNotIn("models.core", source_line)


class TestStackReportLines(GlobalContractsMixin, EngineTestCase):
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
                "assent.inspection.resolve_stack_state",
                side_effect=AssertionError(
                    "must not resolve stack state for a complete folder")):
            lines = inspection._stack_report_lines(cfg, plan)
        self.assertEqual(
            lines, ["Stack base: not applicable (folder complete)"])

    def test_incomplete_folder_still_reports_current_target_main(self):
        self.write_task(1)  # TODO, no upstream declared
        cfg = self.build()
        self.commit_all()
        from assent.plan import Plan
        plan = Plan.parse(cfg.tasks_dir)
        lines = inspection._stack_report_lines(cfg, plan)
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
                "assent.inspection.resolve_stack_state",
                side_effect=AssentError(
                    "upstream folder plan00 has no plan00/* source branch")):
            lines = inspection._stack_report_lines(cfg, plan)
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
        state = preflight.StackState(
            base=FolderBaseResolution(
                target_snapshot="deadbeef", speculative_upstream=upstream,
                resolved_base="abc123"),
            sources=(upstream,))
        with mock.patch(
                "assent.inspection.resolve_stack_state", return_value=state):
            lines = inspection._stack_report_lines(cfg, plan)
        self.assertEqual(lines, [
            "Stack base: abc123",
            "Speculative upstream: plan00 @ abc123 (unaccepted)"])

    def test_status_and_report_show_not_applicable_for_complete_folder(self):
        self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.status(cfg), 0)
        self.assertIn(
            "Stack base: not applicable (folder complete)", out.getvalue())
        from assent.plan import Plan
        text = inspection.render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn(
            "Stack base: not applicable (folder complete)", text)


if __name__ == "__main__":
    unittest.main()
