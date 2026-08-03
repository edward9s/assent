"""Tests for provider-neutral auto-fix review records and derived state."""
import json
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from assent import AssentError
from assent.auto_fix import (
    AUTO_FIX_STATE_VERSION, REVIEW_FINDING_KINDS, ApprovedScopeAddition,
    AutoFixState, FixerProfile, ObservedState, PlanDigestTransition,
    RepairBrief, RepairRoundAssignment, ReviewFinding, ReviewRecord, ReviewTransition,
    ReviewerRecommendation, ScopeAddition, WorkerDisposition,
    auto_fix_state_is_fresh,
    auto_fix_state_path, consume_fixer_profile, current_review_record,
    finding_fingerprint, next_unused_fixer_profile, normalize_finding_path,
    parse_repair_dispositions, parse_review_output, persisted_finding,
    read_auto_fix_state,
    review_record_json, review_record_schema, scheduler_finding_path,
    snapshot_project_surface,
    state_for_review, validate_review_findings, validate_review_transitions,
    validate_scope_additions,
    with_repair_phase,
    write_auto_fix_state,
)


class TestReviewRecord(unittest.TestCase):
    def setUp(self):
        self.finding = ReviewFinding(
            task_id="t001", path="assent\\config.py",
            summary="Review configuration is not validated",
            evidence="The loader accepts an unknown key.",
            recommendation="Reject unknown review configuration keys.",
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
            self.finding.evidence,
            recommendation=self.finding.recommendation)
        self.assertEqual(finding_fingerprint(self.finding),
                         finding_fingerprint(normalized))
        data = json.loads(review_record_json(ReviewRecord("FAIL", (self.finding,))))
        self.assertNotIn("fingerprint", data["findings"][0])

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

    def test_scheduler_rejects_pass_with_any_finding(self):
        with self.assertRaisesRegex(
                AssentError, "PASS auto-fix review must have no blocking findings"):
            review_record_json(ReviewRecord("PASS", (self.finding,)))

    def test_scheduler_rejects_fail_without_findings(self):
        with self.assertRaisesRegex(
                AssentError, "FAIL auto-fix review must have a blocking finding"):
            review_record_json(ReviewRecord("FAIL", ()))

    def test_record_validation_fail_closed(self):
        cases = (
            ReviewRecord("MAYBE", ()),
            ReviewRecord("FAIL", (self.finding, self.finding)),
            ReviewRecord("FAIL", (replace(self.finding, task_id="task-1"),)),
            ReviewRecord("FAIL", (replace(self.finding, path="../outside"),)),
            ReviewRecord("FAIL", (replace(self.finding, summary=" "),)),
            ReviewRecord("FAIL", (replace(self.finding, evidence=""),)),
        )
        for record in cases:
            with self.subTest(record=record), self.assertRaises(AssentError):
                review_record_json(record)

    def test_all_bounded_kinds_and_exact_scope_action_round_trip(self):
        for kind in sorted(REVIEW_FINDING_KINDS - {"scope_amendment"}):
            with self.subTest(kind=kind):
                finding = replace(self.finding, kind=kind)
                parsed = parse_review_output(review_record_json(
                    ReviewRecord("FAIL", (finding,))))
                self.assertEqual(parsed.findings[0], replace(
                    finding, path="assent/config.py"))
        scope = replace(
            self.finding, kind="scope_amendment",
            path="assent/new_config.py",
            scope_addition=ScopeAddition("assent\\new_config.py", "new_file"))
        parsed = parse_review_output(review_record_json(
            ReviewRecord("FAIL", (scope,))))
        self.assertEqual(parsed.findings[0].scope_addition,
                         ScopeAddition("assent/new_config.py", "new_file"))
        for bad in (
                replace(self.finding, kind="complete_verification"),
                replace(self.finding, kind="receipt_absence"),
                replace(self.finding, recommendation=" "),
                replace(self.finding, kind="scope_amendment"),
                replace(scope, scope_addition=ScopeAddition(
                    "assent/new_config.py", "directory"))):
            with self.subTest(bad=bad), self.assertRaises(AssentError):
                review_record_json(ReviewRecord("FAIL", (bad,)))

    def test_recheck_transition_retains_identity_and_separates_new_proof(self):
        first = state_for_review(
            ReviewRecord("FAIL", (self.finding,)), source_tree="1" * 40,
            task_plan_sha256="2" * 64, review_prompt_sha256="3" * 64,
            reviewer_adapter="codex", reviewer_model="review-model",
            reviewer_effort="high", review_stage="initial")
        fingerprint = first.current_finding_fingerprints[0]
        still_present = replace(
            self.finding, transition="still_present",
            prior_fingerprint=fingerprint,
            transition_evidence="The repaired loader still accepts extra keys.")
        validated = validate_review_transitions(
            ReviewRecord("FAIL", (still_present,)), review_stage="recheck",
            previous=first)
        self.assertEqual(finding_fingerprint(validated.findings[0]), fingerprint)
        self.assertNotEqual(
            fingerprint,
            finding_fingerprint(replace(
                self.finding, recommendation="Use a materially different repair.")))

        for invalid in (
                replace(still_present, summary="Changed wording"),
                replace(still_present, prior_fingerprint="f" * 64),
                replace(self.finding, transition="repair_regression",
                        transition_evidence=" "),
                self.finding):
            with self.subTest(invalid=invalid), self.assertRaises(AssentError):
                validate_review_transitions(
                    ReviewRecord("FAIL", (invalid,)), review_stage="recheck",
                    previous=first)

        wording_variant = replace(
            self.finding, summary="Same blocker, cosmetically reworded",
            transition="newly_exposed", prior_fingerprint=None,
            transition_evidence=(
                "t001 acceptance requirement allegedly exposes the same issue."))
        with self.assertRaisesRegex(AssentError, "wording variant"):
            validate_review_transitions(
                ReviewRecord("FAIL", (wording_variant,)),
                review_stage="recheck", previous=first)

        new_finding = replace(
            self.finding, path="assent/auto_fix.py",
            transition="repair_regression", prior_fingerprint=None,
            transition_evidence="The repair diff removed strict record parsing.")
        validate_review_transitions(
            ReviewRecord("FAIL", (new_finding,)), review_stage="recheck",
            previous=first, repair_changed_paths=("assent/auto_fix.py",))
        with self.assertRaisesRegex(AssentError, "repair delta"):
            validate_review_transitions(
                ReviewRecord("FAIL", (new_finding,)), review_stage="recheck",
                previous=first,
                repair_changed_paths=("tests/test_auto_fix.py",))

    def test_codex_provider_schema_is_closed_and_uses_supported_keywords(self):
        schema = review_record_schema()
        supported_keywords = {
            "type", "enum", "additionalProperties", "required", "properties",
            "items", "maxItems", "minLength", "maxLength", "pattern",
        }
        unsupported_composition = {
            "oneOf", "anyOf", "allOf", "not", "if", "then", "else",
            "dependentRequired", "dependentSchemas",
        }

        def visit_schema(node):
            if isinstance(node, dict):
                self.assertTrue(set(node) <= supported_keywords)
                self.assertFalse(set(node) & unsupported_composition)
                properties = node.get("properties", {})
                for child in properties.values():
                    visit_schema(child)
                for key, child in node.items():
                    if key != "properties":
                        visit_schema(child)
            elif isinstance(node, list):
                for child in node:
                    visit_schema(child)

        visit_schema(schema)
        self.assertEqual(schema["type"], "object")
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["required"], ["type", "verdict", "findings"])
        self.assertEqual(set(schema["properties"]), {"type", "verdict", "findings"})
        self.assertNotIn("anyOf", schema)
        self.assertEqual(schema["properties"]["type"]["enum"],
                         ["assent.auto_fix_review"])
        self.assertEqual(schema["properties"]["verdict"]["enum"],
                         ["PASS", "FAIL"])
        self.assertFalse(schema["properties"]["findings"]["items"]
                         ["additionalProperties"])
        self.assertEqual(schema["properties"]["findings"]["maxItems"], 100)
        self.assertEqual(
            schema["properties"]["findings"]["items"]["properties"]["task_id"]
            ["type"], ["string", "null"])

    def test_path_normalization_is_project_relative(self):
        self.assertEqual(normalize_finding_path("a\\b.py"), "a/b.py")
        for path in ("", ".", "/abs", "C:/abs", "a/../b", "a//b"):
            with self.subTest(path=path), self.assertRaises(AssentError):
                normalize_finding_path(path)

    def test_scheduler_directory_scope_becomes_a_canonical_finding_path(self):
        self.assertEqual(scheduler_finding_path("src/"), "src")
        self.assertEqual(scheduler_finding_path("src\\"), "src")

    def test_repair_dispositions_are_exact_complete_and_status_compatible(self):
        first = "1" * 64
        second = "2" * 64
        detail = "\n".join((
            "Implementation notes.",
            'ASSENT_REPAIR_DISPOSITION {"fingerprint":"' + first
            + '","disposition":"fixed","detail":"focused case passes"}',
            'ASSENT_REPAIR_DISPOSITION {"fingerprint":"' + second
            + '","disposition":"not_reproducible","detail":"trace disproves it"}',
        ))
        parsed = parse_repair_dispositions(
            detail, task_id="t001", task_status="DONE",
            expected_fingerprints=(first, second))
        self.assertEqual(
            [(item.fingerprint, item.disposition) for item in parsed],
            [(first, "fixed"), (second, "not_reproducible")])

        invalid = (
            detail.replace(second, first),
            detail.splitlines()[1],
            detail.replace('"detail":"trace disproves it"',
                           '"detail":""'),
            detail.replace('"disposition":"fixed"',
                           '"disposition":"still_blocked"'),
            detail.replace('"detail":"focused case passes"',
                           '"detail":"focused case passes","extra":"x"'),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(AssentError):
                parse_repair_dispositions(
                    value, task_id="t001", task_status="DONE",
                    expected_fingerprints=(first, second))


class TestScopeAdditionValidation(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "existing.py").write_text(
            "value = 1\n", encoding="utf-8")
        self.plan = SimpleNamespace(tasks=(
            SimpleNamespace(id="t001", scope=["src/base.py"]),
            SimpleNamespace(id="t002", scope=["docs/"]),
        ))

    @staticmethod
    def addition(path, state="existing_file", task_id="t001"):
        return ApprovedScopeAddition("a" * 64, task_id, path, state)

    def test_existing_file_and_absent_leaf_are_distinct_valid_actions(self):
        additions = (
            self.addition("src/existing.py"),
            self.addition("tests/new_case.py", "new_file"),
        )
        self.assertEqual(
            validate_scope_additions(self.root, self.plan, additions), additions)

    def test_unsafe_or_mismatched_paths_all_refuse(self):
        (self.root / "src" / "directory").mkdir()
        cases = (
            self.addition("src/missing.py"),
            self.addition("src/existing.py", "new_file"),
            self.addition("missing/new.py", "new_file"),
            self.addition("src/directory"),
            self.addition(".assent/receipt.toml", "new_file"),
            self.addition("AGENTS.md", "new_file"),
            self.addition(".gitignore", "new_file"),
            self.addition("src/*.py", "new_file"),
            self.addition("src/base.py"),
            self.addition("docs/new.py", "new_file"),
        )
        for addition in cases:
            with self.subTest(path=addition.path), self.assertRaises(AssentError):
                validate_scope_additions(self.root, self.plan, (addition,))

    def test_nested_instruction_and_git_control_files_are_protected(self):
        # Each nested target has an existing ordinary parent directory, so only
        # the depth-independent basename rule can refuse it.
        cases = (
            self.addition("src/AGENTS.md", "new_file"),
            self.addition("src/.gitignore", "new_file"),
            self.addition("tests/.gitattributes", "new_file"),
            self.addition("tests/.gitmodules", "new_file"),
        )
        for addition in cases:
            with self.subTest(path=addition.path), self.assertRaisesRegex(
                    AssentError, "protected control surface"):
                validate_scope_additions(self.root, self.plan, (addition,))

    def test_link_mediated_parent_refuses_without_traversal(self):
        target = self.root / "outside"
        target.mkdir()
        link = self.root / "linked"
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError as e:
            self.skipTest(f"directory symlink unavailable: {e}")
        with self.assertRaisesRegex(AssentError, "link or reparse"):
            validate_scope_additions(
                self.root, self.plan,
                (self.addition("linked/new.py", "new_file"),))


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
            recommendation="Validate every persisted state field.",
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
            reviewer_recommendations=(ReviewerRecommendation(
                finding.fingerprint, finding.recommendation),),
            worker_dispositions=(WorkerDisposition(
                "t001", finding.fingerprint, "fixed",
                "The focused regression now passes."),),
            repair_briefs=(RepairBrief(
                "t001", (finding.fingerprint,),
                "Validate and round-trip the complete version-5 schema."),),
            review_transitions=(ReviewTransition(
                finding.fingerprint, "initial", None, None),),
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

    def test_not_started_round_assignment_round_trips_with_consumed_profile(self):
        profile = self.state.consumed_fixer_profiles[0]
        state = replace(
            self.state, phase="REPAIRING",
            repair_round_assignments=(RepairRoundAssignment(
                "t001", profile.adapter, profile.model, profile.effort,
                attempted=False),))
        write_auto_fix_state(self.path, state)
        self.assertEqual(read_auto_fix_state(self.path), state)

        missing_profile = replace(
            state, consumed_fixer_profiles=())
        with self.assertRaisesRegex(AssentError, "absent from consumed history"):
            write_auto_fix_state(self.path, missing_profile)

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
            current_finding_fingerprints=(), reviewer_recommendations=())
        write_auto_fix_state(self.path, passed)
        self.assertEqual(read_auto_fix_state(self.path), passed)

    def test_only_an_exact_pass_is_fresh(self):
        passed = replace(
            self.state, phase="COMPLETE", verdict="PASS",
            current_finding_fingerprints=(), reviewer_recommendations=())
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

    def test_blocked_context_actions_and_plan_transitions_round_trip(self):
        scope_finding = ReviewFinding(
            "t001", "tests/new_case.py", "Focused coverage is outside scope",
            "The blocked worker named the missing regression file.",
            kind="scope_amendment",
            recommendation="Add the exact test file to t001 scope.",
            scope_addition=ScopeAddition("tests/new_case.py", "new_file"))
        state = state_for_review(
            ReviewRecord("FAIL", (scope_finding,)), source_tree=self.tree,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", review_context="blocked_adjudication",
            review_stage="initial", failure_trigger="worker_blocked",
            worker_dispositions=(WorkerDisposition(
                "t001", finding_fingerprint(scope_finding), "still_blocked",
                "The exact required path was outside scope."),))
        fingerprint = state.current_finding_fingerprints[0]
        self.assertEqual(state.approved_scope_additions, (
            ApprovedScopeAddition(
                fingerprint, "t001", "tests/new_case.py", "new_file"),))

        updated = state_for_review(
            ReviewRecord("PASS", ()), source_tree="4" * 40,
            task_plan_sha256="5" * 64,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", previous=state,
            review_context="blocked_adjudication", review_stage="recheck",
            failure_trigger="worker_blocked")
        self.assertEqual(updated.plan_digest_transitions, (
            PlanDigestTransition(self.plan_digest, "5" * 64),))
        self.assertEqual(updated.consumed_fixer_profiles,
                         state.consumed_fixer_profiles)
        write_auto_fix_state(self.path, updated)
        self.assertEqual(read_auto_fix_state(self.path), updated)

    def test_technical_debt_is_only_initial_completed_folder_evidence(self):
        finding = ReviewFinding(
            "t001", "assent/auto_fix.py", "Interacting debt blocks safety",
            "The initial cumulative-diff review exposed the defect.",
            kind="eligible_technical_debt",
            recommendation="Repair the local defect and retain it for acceptance.")
        state_for_review(
            ReviewRecord("FAIL", (finding,)), source_tree=self.tree,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", review_stage="initial")
        with self.assertRaisesRegex(AssentError, "technical debt"):
            state_for_review(
                ReviewRecord("FAIL", (finding,)), source_tree=self.tree,
                task_plan_sha256=self.plan_digest,
                review_prompt_sha256=self.prompt_digest,
                reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
                reviewer_effort="high", review_context="blocked_adjudication",
                review_stage="initial", failure_trigger="focused_gate_failure")

        fingerprint = finding_fingerprint(finding)
        retained = ReviewFinding(
            finding.task_id, finding.path, finding.summary, finding.evidence,
            kind=finding.kind, recommendation=finding.recommendation,
            transition="still_present", prior_fingerprint=fingerprint,
            transition_evidence="The same local debt remains after repair.")
        rechecked = state_for_review(
            ReviewRecord("FAIL", (retained,)), source_tree="4" * 40,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", previous=state_for_review(
                ReviewRecord("FAIL", (finding,)), source_tree=self.tree,
                task_plan_sha256=self.plan_digest,
                review_prompt_sha256=self.prompt_digest,
                reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
                reviewer_effort="high", review_stage="initial"),
            review_stage="recheck")
        self.assertEqual(rechecked.current_finding_fingerprints, (fingerprint,))
        resolved = state_for_review(
            ReviewRecord("PASS", ()), source_tree="5" * 40,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", previous=rechecked,
            review_stage="recheck")
        self.assertEqual(resolved.current_finding_fingerprints, ())

        novel = ReviewFinding(
            "t001", "assent/auto_fix.py", "Different debt",
            "A recheck attempted to introduce another debt identity.",
            kind="eligible_technical_debt",
            transition="repair_regression",
            transition_evidence="The repair changed assent/auto_fix.py.")
        with self.assertRaisesRegex(AssentError, "technical debt"):
            state_for_review(
                ReviewRecord("FAIL", (novel,)), source_tree="6" * 40,
                task_plan_sha256=self.plan_digest,
                review_prompt_sha256=self.prompt_digest,
                reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
                reviewer_effort="high", previous=rechecked,
                review_stage="recheck")

    def test_recheck_retains_ledger_scope_approvals_and_profile_history(self):
        scope_finding = ReviewFinding(
            "t001", "tests/new_case.py", "A scoped test is required",
            "The task cannot add the required regression.",
            kind="scope_amendment", recommendation="Authorize this test file.",
            scope_addition=ScopeAddition("tests/new_case.py", "new_file"))
        first = state_for_review(
            ReviewRecord("FAIL", (scope_finding,)), source_tree=self.tree,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", review_stage="initial")
        first = consume_fixer_profile(
            first, FixerProfile("codex", "prime", "heavy"))
        changed = ReviewFinding(
            "t001", "assent/auto_fix.py", "Repair introduced a regression",
            "The changed parser now accepts an unknown finding key.",
            recommendation="Restore exact-key validation.",
            transition="repair_regression",
            transition_evidence="The repair diff changed _FINDING_KEYS.")
        second = state_for_review(
            ReviewRecord("FAIL", (changed,)), source_tree="4" * 40,
            task_plan_sha256=self.plan_digest,
            review_prompt_sha256=self.prompt_digest,
            reviewer_adapter="codex", reviewer_model="gpt-5.6-sol",
            reviewer_effort="high", previous=first, review_stage="recheck")
        self.assertEqual(len(second.findings), 2)
        self.assertEqual(second.approved_scope_additions,
                         first.approved_scope_additions)
        self.assertEqual(second.consumed_fixer_profiles,
                         first.consumed_fixer_profiles)

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

    def test_review_surface_protects_exact_root_management_state(self):
        source = self.root / "source"
        management = self.root / ".assent"
        folder = management / "plan01"
        source.mkdir()
        folder.mkdir(parents=True)
        protected = tuple(management / name for name in (
            "manifest.toml", "_batch_verification.toml", "_archived.toml",
            "_archive"))
        before = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=protected)

        for path in protected:
            if path.name == "_archive":
                path.mkdir()
                (path / "plan00.zip").write_bytes(b"reviewer archive mutation")
            else:
                path.write_text("mutated during review\n", encoding="utf-8")
        changed = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=protected)
        self.assertEqual(before.changed_paths(changed), (
            "management:_archive",
            "management:_archive:plan00.zip",
            "management:_archived.toml",
            "management:_batch_verification.toml",
            "management:manifest.toml",
        ))

    def test_review_surface_ignores_unselected_root_runtime_output(self):
        source = self.root / "source"
        management = self.root / ".assent"
        folder = management / "plan01"
        source.mkdir()
        folder.mkdir(parents=True)
        protected = (management / "manifest.toml",)
        before = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=protected)

        (management / "parallel-runtime.tmp").write_text(
            "unrelated scheduler output\n", encoding="utf-8")
        after = snapshot_project_surface(
            source, management, tasks_dir=folder,
            stable_management_files=protected)
        self.assertEqual(before.changed_paths(after), ())


if __name__ == "__main__":
    unittest.main()
