"""Tests for provider-neutral auto-fix review records and derived state."""
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from assent import AssentError
from assent.auto_fix import (
    AUTO_FIX_STATE_VERSION, AutoFixState, FixerProfile, ObservedState,
    ReviewFinding, ReviewRecord, auto_fix_state_is_fresh,
    auto_fix_state_path, consume_fixer_profile, current_review_record,
    finding_fingerprint, next_unused_fixer_profile, normalize_finding_path,
    parse_review_output, persisted_finding, read_auto_fix_state,
    review_record_json, scheduler_finding_path, snapshot_project_surface,
    state_for_review, validate_review_findings, with_repair_phase,
    write_auto_fix_state,
)


class TestReviewRecord(unittest.TestCase):
    def setUp(self):
        self.finding = ReviewFinding(
            task_id="t001", path="assent\\config.py",
            summary="Review configuration is not validated",
            evidence="The loader accepts an unknown key.",
        )

    def test_pass_and_fail_round_trip_deterministically(self):
        records = (
            ReviewRecord("PASS", ()),
            ReviewRecord("FAIL", (self.finding,)),
        )
        for record in records:
            with self.subTest(verdict=record.verdict):
                text = review_record_json(record)
                parsed = parse_review_output("adapter preamble\n" + text + "\n")
                self.assertEqual(review_record_json(parsed), text)
        self.assertEqual(parsed.findings[0].path, "assent/config.py")

    def test_findings_require_one_existing_scope_owner(self):
        plan = SimpleNamespace(tasks=(
            SimpleNamespace(id="t001", scope=["assent/"]),
            SimpleNamespace(id="t002", scope=["tests/"]),
        ))
        record = validate_review_findings(
            ReviewRecord("FAIL", (replace(
                self.finding, task_id=None, path="assent/config.py"),)), plan)
        self.assertEqual(record.findings[0].task_id, "t001")
        with self.assertRaisesRegex(AssentError, "outside.*declared scope"):
            validate_review_findings(
                ReviewRecord("FAIL", (replace(
                    self.finding, path="tests/test_config.py"),)), plan)

    def test_fingerprints_are_scheduler_computed_and_stable(self):
        normalized = ReviewFinding(
            "t001", "assent/config.py", self.finding.summary,
            self.finding.evidence)
        self.assertEqual(finding_fingerprint(self.finding),
                         finding_fingerprint(normalized))
        data = review_record_json(ReviewRecord("FAIL", (self.finding,)))
        self.assertNotIn("fingerprint", data)

    def test_missing_duplicate_malformed_and_trailing_records_refuse(self):
        valid = review_record_json(ReviewRecord("PASS", ()))
        cases = (
            "ordinary adapter output\n",
            valid + "\n" + valid,
            '{"type":"assent.auto_fix_review",broken}\n',
            valid + "\ntrailing output\n",
            '{"type":"assent.auto_fix_review","verdict":"PASS"}\n',
            ('{"type":"assent.auto_fix_review","verdict":[],"findings":[]}'
             '\n'),
        )
        for output in cases:
            with self.subTest(output=output[:40]), self.assertRaises(AssentError):
                parse_review_output(output)

    def test_record_schema_and_verdict_consistency_fail_closed(self):
        cases = (
            ReviewRecord("MAYBE", ()),
            ReviewRecord("PASS", (self.finding,)),
            ReviewRecord("FAIL", ()),
            ReviewRecord("FAIL", (self.finding, self.finding)),
            ReviewRecord("FAIL", (replace(self.finding, task_id="task-1"),)),
            ReviewRecord("FAIL", (replace(self.finding, path="../outside"),)),
            ReviewRecord("FAIL", (replace(self.finding, summary=" "),)),
            ReviewRecord("FAIL", (replace(self.finding, evidence=""),)),
        )
        for record in cases:
            with self.subTest(record=record), self.assertRaises(AssentError):
                review_record_json(record)

    def test_path_normalization_is_project_relative(self):
        self.assertEqual(normalize_finding_path("a\\b.py"), "a/b.py")
        for path in ("", ".", "/abs", "C:/abs", "a/../b", "a//b"):
            with self.subTest(path=path), self.assertRaises(AssentError):
                normalize_finding_path(path)

    def test_scheduler_directory_scope_becomes_a_canonical_finding_path(self):
        self.assertEqual(scheduler_finding_path("src/"), "src")
        self.assertEqual(scheduler_finding_path("src\\"), "src")


class TestAutoFixState(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.path = self.root / ".assent" / "plan01" / "_auto_fix.toml"
        self.tree = "1" * 40
        self.plan_digest = "2" * 64
        self.prompt_digest = "3" * 64
        finding = persisted_finding(ReviewFinding(
            "t001", "assent/auto_fix.py", "State validation is missing",
            "A malformed state was accepted.",
        ))
        self.state = AutoFixState(
            version=AUTO_FIX_STATE_VERSION,
            source_tree=self.tree,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex",
            reviewer_model="gpt-5.6-sol",
            reviewer_effort="high",
            phase="NEEDS_REPAIR",
            verdict="FAIL",
            current_finding_fingerprints=(finding.fingerprint,),
            findings=(finding,),
            observed_states=(ObservedState(
                self.tree, (finding.fingerprint,)),),
            consumed_fixer_profiles=(FixerProfile(
                "codex", "core", "normal"),),
        )

    def test_state_round_trip_preserves_ledger_history_and_profiles(self):
        write_auto_fix_state(self.path, self.state)
        loaded = read_auto_fix_state(self.path)
        self.assertEqual(loaded, self.state)
        self.assertEqual(loaded.finding_ledger, self.state.findings)
        self.assertEqual(loaded.observed_states, self.state.observed_states)
        self.assertEqual(loaded.consumed_fixer_profiles,
                         self.state.consumed_fixer_profiles)
        self.assertFalse(any(
            child.name.endswith(".tmp")
            for child in self.path.parent.iterdir()))

    def test_profile_cursor_is_deduplicated_and_current_record_is_recoverable(self):
        candidates = (
            self.state.consumed_fixer_profiles[0],
            FixerProfile("claude", "prime", "heavy"),
            FixerProfile("claude", "prime", "heavy"),
        )
        selected = next_unused_fixer_profile(self.state, candidates)
        self.assertEqual(selected, FixerProfile("claude", "prime", "heavy"))
        consumed = consume_fixer_profile(self.state, selected)
        self.assertIsNone(next_unused_fixer_profile(consumed, candidates))
        self.assertEqual(current_review_record(consumed).findings[0],
                         self.state.findings[0].finding)
        with self.assertRaisesRegex(AssentError, "duplicate consumed"):
            consume_fixer_profile(consumed, selected)
        self.assertEqual(
            with_repair_phase(consumed, "AWAITING_REVIEW").phase,
            "AWAITING_REVIEW")

    def test_atomic_replacement_writes_one_complete_new_state(self):
        write_auto_fix_state(self.path, self.state)
        passed = replace(
            self.state, phase="COMPLETE", verdict="PASS",
            current_finding_fingerprints=())
        write_auto_fix_state(self.path, passed)
        self.assertEqual(read_auto_fix_state(self.path), passed)

    def test_only_an_exact_pass_is_fresh(self):
        passed = replace(
            self.state, phase="COMPLETE", verdict="PASS",
            current_finding_fingerprints=())
        identity = dict(
            source_tree=self.tree,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex",
            reviewer_model="gpt-5.6-sol",
            reviewer_effort="high",
        )
        self.assertTrue(auto_fix_state_is_fresh(passed, **identity))
        self.assertFalse(auto_fix_state_is_fresh(self.state, **identity))
        for key in identity:
            changed = dict(identity)
            changed[key] = ("4" * 40 if key == "source_tree"
                            else "4" * 64 if key.endswith("sha256")
                            else "different")
            with self.subTest(key=key):
                self.assertFalse(auto_fix_state_is_fresh(passed, **changed))

    def test_malformed_state_refuses_instead_of_becoming_a_cache_miss(self):
        write_auto_fix_state(self.path, self.state)
        original = self.path.read_text(encoding="utf-8")
        cases = (
            "not valid toml = [\n",
            original + "unknown = true\n",
            original.replace('verdict = "FAIL"', 'verdict = "PASS"'),
            original.replace(self.state.findings[0].fingerprint, "f" * 64, 1),
        )
        for index, text in enumerate(cases):
            with self.subTest(index=index):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(AssentError):
                    read_auto_fix_state(self.path)

    def test_invalid_state_never_replaces_existing_bytes(self):
        write_auto_fix_state(self.path, self.state)
        before = self.path.read_bytes()
        invalid = replace(self.state, source_tree="not-a-tree")
        with self.assertRaises(AssentError):
            write_auto_fix_state(self.path, invalid)
        self.assertEqual(self.path.read_bytes(), before)

    def test_state_path_is_folder_local(self):
        self.assertEqual(auto_fix_state_path(self.root / ".assent" / "plan01"),
                         self.path)

    def test_new_pass_with_empty_ledger_round_trips(self):
        state = state_for_review(
            ReviewRecord("PASS", ()), source_tree=self.tree,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high")
        write_auto_fix_state(self.path, state)
        self.assertEqual(read_auto_fix_state(self.path), state)

    def test_surface_snapshot_reports_exact_changed_paths(self):
        source = self.root / "source"
        management = self.root / ".assent"
        source.mkdir()
        management.mkdir(exist_ok=True)
        (source / "code.py").write_text("before\n", encoding="utf-8")
        before = snapshot_project_surface(source, management)
        (source / "code.py").write_text("after\n", encoding="utf-8")
        (management / "new.toml").write_text("value = 1\n", encoding="utf-8")
        after = snapshot_project_surface(source, management)
        self.assertEqual(before.changed_paths(after), (
            "management:new.toml", "source:code.py"))

    def test_review_surface_excludes_log_and_unrelated_folder(self):
        source = self.root / "source"
        management = self.root / ".assent"
        folder = management / "plan01"
        unrelated = management / "plan02"
        source.mkdir()
        folder.mkdir(parents=True)
        unrelated.mkdir()
        task = folder / "t001_task.e.toml"
        verifier = management / "verify.py"
        task.write_text('status = "DONE"\n', encoding="utf-8")
        verifier.write_text("before\n", encoding="utf-8")
        before = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=(verifier,))

        (folder / "_assent.log").write_text("runtime output\n", encoding="utf-8")
        (unrelated / "t001_task.r.toml").write_text(
            "parallel progress\n", encoding="utf-8")
        unchanged = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=(verifier,))
        self.assertEqual(before.changed_paths(unchanged), ())

        task.write_text('status = "BLOCKED"\n', encoding="utf-8")
        verifier.write_text("after\n", encoding="utf-8")
        changed = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=(verifier,))
        self.assertEqual(before.changed_paths(changed), (
            "management:plan01:t001_task.e.toml", "management:verify.py"))


if __name__ == "__main__":
    unittest.main()
