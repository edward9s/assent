"""``assent verify --batch`` execution tests.

Which plans enter one candidate, in what order they are merged, what the
resulting batch receipt certifies, how a conflicting plan is skipped or
refused, and how a failed batch is localized to the single plan that breaks
it.  These tests run against disposable local repositories rather than mocks,
because the facts under test are Git facts.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest

from tests.engine_support import ScriptedAdapter, models_block
from pathlib import Path
from unittest import mock

from assent import engine, gitops, shared_paths
from assent.adapters import TaskResult
from assent.__main__ import _dispatch
from assent.batch_accept import accept_all
from assent.batch_receipt import (BatchVerificationReceipt, batch_receipt_path,
                                  read_batch_receipt)
from assent.batch_verification import (SelectionConflictEvidence,
                                       confirm_on_terminal, verify_batch,
                                       verify_selected_batch,
                                       verify_selected_batch_action)
from assent.config import load_config
from assent.plan_verification import receipt_path
from assent.init import _BRIDGE_LINE, _EXPANDED_BRIDGE_LINE
from assent.lockfile import hold_integration_lock, hold_lock
from assent.verification_common import build_batch_candidate
from tests.test_shared_paths import excluded_inventory
from tests.test_verification import make_directory_link

_VERIFY_OK = "raise SystemExit(0)\n"
_VERIFY_FAILS = "print('two tests failed')\nraise SystemExit(3)\n"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class BatchVerifyRepositoryCase(unittest.TestCase):
    """A trunk repository plus helpers for building finished source plans."""

    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent batch verify test "))
        self.questions: list[str] = []
        self.root = self.parent / "repository"
        self.root.mkdir()
        self.addCleanup(self._cleanup)
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Assent Test")
        _git(self.root, "config", "user.email", "assent@example.invalid")
        _git(self.root, "checkout", "-b", "trunk")
        # pkg/ and assets/ let a test provision a real root-level directory
        # link that stays ignored in the source worktree and the candidate;
        # lib/ adds the nested cases, a directory link at lib/l10n/arb and an
        # ignored generated leaf beside the tracked lib/models/task.dart.
        (self.root / ".gitignore").write_text(
            ".assent/\npkg/\nassets/\nignored/\nlib/l10n/arb/\n*.g.dart\n",
            encoding="utf-8")
        (self.root / "README.md").write_text("initial\n", encoding="utf-8")
        (self.root / "lib" / "models").mkdir(parents=True)
        (self.root / "lib" / "models" / "task.dart").write_text(
            "tracked source\n", encoding="utf-8")
        (self.root / "lib" / "l10n").mkdir(parents=True)
        (self.root / "lib" / "l10n" / "app_en.arb").write_text(
            "{}\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")

        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text(
            '[workflow]\ntask = [{ action = "focused_test" }]\n'
            + models_block(), encoding="utf-8")
        self.write_verify(_VERIFY_OK)

    def _cleanup(self) -> None:
        if self.root.exists():
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=self.root,
                capture_output=True, encoding="utf-8", errors="replace")
            for line in result.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                path = Path(line.removeprefix("worktree "))
                if path.resolve() != self.root.resolve():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(path)],
                        cwd=self.root, capture_output=True)
        shutil.rmtree(self.parent, ignore_errors=True)

    def write_verify(self, text: str) -> None:
        (self.assent_dir / "verify.py").write_text(text, encoding="utf-8")

    def write_task(self, plan_name: str, status: str = "DONE") -> Path:
        tasks_dir = self.assent_dir / plan_name
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / "t001_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "core"\n'
            f'status = "{status}"\n'
            'verify = "python --version"\n'
            'goal = "Complete the task."\n'
            'acceptance = "Verification passes."\n',
            encoding="utf-8")
        return path

    def write_after(self, plan_name: str, after: tuple[str, ...]) -> None:
        values = ", ".join(f'"{item}"' for item in after)
        (self.assent_dir / plan_name / "_plan_deps.toml").write_text(
            f"after = [{values}]\n", encoding="utf-8")

    def make_source(self, plan_name: str, *, filename: str | None = None,
                    content: str = "result\n", status: str = "DONE") -> str:
        """Create a finished plan with one commit on its own source branch."""
        self.write_task(plan_name, status=status)
        worktree = gitops.ensure_worktree(self.root, plan_name)
        branch = gitops.ensure_branch(worktree, f"{plan_name}/")
        (worktree / (filename or f"{plan_name}.txt")).write_text(
            content, encoding="utf-8")
        gitops.commit_all(worktree, f"finish {plan_name}")
        return gitops.branch_tip(self.root, branch)

    def head(self, ref: str = "HEAD") -> str:
        return _git(self.root, "rev-parse", ref)

    def receipt_path(self) -> Path:
        return batch_receipt_path(self.assent_dir)

    def read_batch_receipt(self) -> BatchVerificationReceipt:
        return read_batch_receipt(self.receipt_path(), self.root)

    def run_batch(self, bisect: bool = True,
                  answer: bool = False) -> tuple[int, str]:
        """Run one batch, recording every conflict-skip question it asks.

        The default answer is no, so a test that expects a verified batch also
        proves no question was asked unless it says otherwise.
        """
        def confirm(question: str) -> bool:
            self.questions.append(question)
            return answer

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_batch(
                str(self.config_path), self.assent_dir, bisect, confirm)
        return code, output.getvalue()

    def run_selected(self, *plan_names: str, bisect: bool = True
                     ) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_selected_batch(
                str(self.config_path), self.assent_dir, plan_names, bisect)
        return code, output.getvalue()


class TestBatchSelection(BatchVerifyRepositoryCase):
    def test_no_plan_at_all_is_an_empty_batch_with_no_receipt(self) -> None:
        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertIn("no plan has anything left to verify", output)
        self.assertFalse(self.receipt_path().exists())

    def test_unfinished_and_source_less_plans_are_skipped_not_failed(self
                                                                      ) -> None:
        for status in ("TODO", "WIP", "BLOCKED"):
            self.write_task(f"plan-{status.lower()}", status=status)
        self.write_task("cleaned")  # DONE, but no branch and no worktree

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        for status in ("TODO", "WIP", "BLOCKED"):
            self.assertIn(f"skip plan-{status.lower()}", output)
        self.assertIn("skip cleaned (no source branch remains", output)
        self.assertIn("no plan has anything left to verify", output)
        self.assertFalse(self.receipt_path().exists())

    def test_source_already_contained_in_the_target_is_skipped(self) -> None:
        alpha_tip = self.make_source("alpha")
        _git(self.root, "merge", "--no-ff", alpha_tip, "-m", "publish alpha")

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertIn("skip alpha", output)
        self.assertIn("already", output)
        self.assertFalse(self.receipt_path().exists())

    def test_merge_order_is_dependency_first_then_lexicographic(self) -> None:
        # Lexicographically the plans are alpha, mike, zulu; the declared
        # dependency must push alpha behind zulu.
        self.make_source("alpha")
        self.make_source("mike")
        self.make_source("zulu")
        self.write_after("alpha", ("zulu",))

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.read_batch_receipt().plan_names,
                         ("mike", "zulu", "alpha"))
        self.assertIn("mike, zulu, alpha", output)

    def test_ordering_is_stable_across_repeated_runs(self) -> None:
        for plan_name in ("delta", "bravo", "charlie"):
            self.make_source(plan_name)
        self.write_after("bravo", ("delta",))

        orders = []
        for _ in range(2):
            code, output = self.run_batch()
            self.assertEqual(code, 0, output)
            orders.append(self.read_batch_receipt().plan_names)

        self.assertEqual(orders[0], ("charlie", "delta", "bravo"))
        self.assertEqual(orders[0], orders[1])


class TestBatchCandidateAndReceipt(BatchVerifyRepositoryCase):
    def test_passed_receipt_records_every_reproducible_step_tree(self) -> None:
        first = self.make_source("aa")
        second = self.make_source("bb")
        target_tip = self.head()

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.exit_code, 0)
        self.assertEqual(receipt.failure_summary, "")
        self.assertEqual(receipt.target_tip, target_tip)
        self.assertEqual([(s.plan, s.source_tip) for s in receipt.sources],
                         [("aa", first), ("bb", second)])
        # The recorded trees must be exactly what rebuilding the same chain
        # produces, which is the whole point of storing every step.
        rebuilt = build_batch_candidate(
            self.root, target_tip, [("aa", first), ("bb", second)])
        self.assertTrue(rebuilt.ok)
        self.assertEqual([s.step_tree for s in receipt.sources],
                         list(rebuilt.step_trees))
        self.assertEqual(receipt.final_tree, rebuilt.step_trees[-1])
        self.assertEqual(self.head(), target_tip)

    def test_failing_verifier_writes_a_failed_receipt_with_the_summary(self
                                                                      ) -> None:
        self.make_source("aa")
        self.write_verify(_VERIFY_FAILS)

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertEqual(receipt.exit_code, 3)
        self.assertIn("two tests failed", receipt.failure_summary)
        self.assertIn("exit code 3", receipt.failure_summary)
        self.assertIn("verify --batch: failed", output)

    def test_non_ascii_failure_survives_batch_receipt(self) -> None:
        self.make_source("aa")
        self.write_verify(
            "import sys\n"
            "print('繁體中文批次標準輸出')\n"
            "print('繁體中文批次錯誤輸出', file=sys.stderr)\n"
            "raise SystemExit(3)\n")

        code, output = self.run_batch(bisect=False)

        self.assertEqual(code, 1, output)
        receipt = self.read_batch_receipt()
        for marker in ("繁體中文批次標準輸出", "繁體中文批次錯誤輸出"):
            with self.subTest(marker=marker):
                self.assertIn(marker, receipt.failure_summary)
        self.assertNotIn("\ufffd", receipt.failure_summary)

    def test_invalid_verifier_output_survives_batch_receipt(self) -> None:
        self.make_source("aa")
        self.write_verify(
            "import os\n"
            "os.write(1, b'raw batch stdout ' + bytes([0x80]) + b'\\n')\n"
            "os.write(2, b'raw batch stderr ' + bytes([0xff]) + b'\\n')\n"
            "raise SystemExit(3)\n")

        code, output = self.run_batch(bisect=False)

        self.assertEqual(code, 1, output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertEqual(receipt.exit_code, 3)
        self.assertIn("not valid UTF-8", receipt.failure_summary)
        self.assertIn(r"\x80", receipt.failure_summary)
        self.assertIn(r"\xff", receipt.failure_summary)
        self.assertNotIn("\ufffd", receipt.failure_summary)
        self.assertNotIn("UnicodeDecodeError", receipt.failure_summary)
        self.assertNotIn("Traceback", receipt.failure_summary)

    def test_conflicting_plan_is_named_and_no_receipt_is_written(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")
        target_tip = self.head()

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("merging bb into the batch candidate conflicts", output)
        self.assertIn("shared.txt", output)
        self.assertFalse(self.receipt_path().exists())
        self.assertEqual(self.head(), target_tip)

    def test_a_conflict_never_overwrites_an_earlier_passed_receipt(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        code, output = self.run_batch()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.receipt_path().exists())

        self.make_source("bb", filename="shared.txt", content="from bb\n")
        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("no receipt was written", output)
        # The earlier receipt is invalidated before the new candidate is built,
        # so a conflicting batch leaves behind no receipt that could still
        # authorize a release of the plans it used to cover.
        self.assertFalse(self.receipt_path().exists())

    def test_batch_leaves_single_plan_receipts_untouched(self) -> None:
        self.make_source("aa")
        cfg = load_config(str(self.config_path), "aa")
        plan_receipt = receipt_path(cfg)
        plan_receipt.write_text("placeholder\n", encoding="utf-8")

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(plan_receipt.read_text(encoding="utf-8"),
                         "placeholder\n")


class TestExplicitBatchSelection(BatchVerifyRepositoryCase):
    def test_selected_verify_recovers_known_init_bridge_drift(self) -> None:
        agents = self.root / "AGENTS.md"
        agents.write_text("# Project\n\n" + _BRIDGE_LINE + "\n",
                          encoding="utf-8")
        _git(self.root, "add", "AGENTS.md")
        _git(self.root, "commit", "-m", "add project rules")
        self.make_source("aa")
        self.make_source("bb")
        agents.write_text("# Project\n\n" + _EXPANDED_BRIDGE_LINE + "\n",
                          encoding="utf-8")

        code, output = self.run_selected("aa", "bb")

        self.assertEqual(code, 0, output)
        self.assertIn("Recovered an Assent-generated AGENTS.md bridge update",
                      output)
        self.assertEqual(_git(self.root, "status", "--porcelain"), "")

    def test_selection_action_collects_the_complete_conflict_wave_before_verify(
            self) -> None:
        self.make_source("aa", filename="one.txt", content="from aa\n")
        self.make_source("bb", filename="one.txt", content="from bb\n")
        self.make_source("cc", filename="two.txt", content="from cc\n")
        self.make_source("dd", filename="two.txt", content="from dd\n")
        self.make_source("ee")
        self.write_after("ee", ("bb",))

        with mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            result = verify_selected_batch_action(
                str(self.config_path), self.root / ".assent",
                ("aa", "bb", "cc", "dd", "ee"))

        self.assertIsInstance(result, SelectionConflictEvidence)
        self.assertEqual(result.outcome, "PEER_CONFLICT")
        self.assertEqual(
            [(item.plan, item.paths, item.kind)
             for item in result.conflicts],
            [("bb", ("one.txt",), "peer_only"),
             ("dd", ("two.txt",), "peer_only")])
        self.assertEqual(result.conflicts[0].dependent_exclusions, ("ee",))
        self.assertEqual(
            tuple(plan_name for plan_name, _tip
                  in result.conflicts[1].prefix_sources), ("aa", "cc"))
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())

    def test_integration_role_repairs_a_peer_conflict_then_rebuilds_selection(
            self) -> None:
        workflow = """
[adapter]
name = "claude"
[abilities.repair]
prompt = "Repair the exact selection conflict from the requirements."
writes = true
[roles.repairer]
ability = ["repair"]
model = "lite"
[workflow]
task = [{ action = "focused_test" }]
plan = []
integration = [{ action = "full_verify" }, { role = "repairer" },
               { action = "full_verify" }]
"""
        self.config_path.write_text(
            workflow + models_block(workflow), encoding="utf-8")
        self.make_source("aa", filename="README.md", content="shared\n")
        bb_before = self.make_source(
            "bb", filename="README.md", content="from bb\n")
        bb_worktree = gitops.plan_worktree(self.root, "bb")
        self.assertIsNotNone(bb_worktree)
        assert bb_worktree is not None

        def repair(prompt):
            self.assertIn(str(bb_worktree), prompt)
            self.assertIn("bb: peer_only", prompt)
            (bb_worktree / "README.md").write_text(
                "shared\n", encoding="utf-8")
            return TaskResult(0, "made bb compatible with the exact selection",
                              False, None)

        adapter = ScriptedAdapter([repair])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), \
                mock.patch("assent.engine.get_adapter", return_value=adapter):
            code = engine.run_selection_workflow(
                str(self.config_path), self.assent_dir, ("aa", "bb"))

        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(len(adapter.calls), 1)
        self.assertNotEqual(gitops.commit_of(bb_worktree, "HEAD"), bb_before)
        self.assertEqual(
            (bb_worktree / "README.md").read_text(encoding="utf-8"),
            "shared\n")
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "bb"))

    def test_selected_names_are_normalized_and_receipt_is_exact(self) -> None:
        parent = self.make_source("parent")
        child = self.make_source("child")
        self.write_after("child", ("parent",))

        target_tip = self.head()
        code, output = self.run_selected("child", "parent")

        self.assertEqual(code, 0, output)
        self.assertIn("verify selected: merging", output)
        self.assertNotIn("verify --batch:", output)
        self.assertIn("merging 2 plan(s) in dependency order: parent, child",
                      output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.plan_names, ("parent", "child"))
        self.assertEqual(
            [(source.plan, source.source_tip) for source in receipt.sources],
            [("parent", parent), ("child", child)])
        self.assertEqual(self.head(), target_tip)

    def test_unselected_live_prerequisite_refuses_before_full_verifier(self) -> None:
        self.make_source("parent", status="TODO")
        self.make_source("child")
        self.make_source("sibling")
        self.write_after("child", ("parent",))

        with mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_selected("child", "sibling")

        self.assertEqual(code, 1)
        self.assertIn("prerequisite parent", output)
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())

    def test_selected_conflict_invalidates_old_receipt_without_question(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        first_code, first_output = self.run_batch()
        self.assertEqual(first_code, 0, first_output)
        old_receipt = self.receipt_path().read_bytes()

        self.make_source("bb", filename="shared.txt", content="from bb\n")
        target_tip = self.head()
        branch_tips = {
            branch: _git(self.root, "rev-parse", branch)
            for branch in _git(
                self.root, "for-each-ref", "--format=%(refname:short)",
                "refs/heads/").splitlines()
        }
        with mock.patch("assent.batch_verification.confirm_on_terminal") as ask, \
                mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_selected("aa", "bb")

        self.assertEqual(code, 1)
        self.assertIn("candidate construction encountered merge conflicts", output)
        self.assertIn("complete exact selection remains required", output)
        self.assertIn("verify selected:", output)
        self.assertNotIn("verify --batch:", output)
        self.assertIn("full verifier did not run", output)
        self.assertIn("shared.txt", output)
        self.assertIn("no receipt was written", output)
        self.assertIn("target ref", output)
        self.assertIn("every selected source ref", output)
        self.assertIn("compatible selected prefix ahead of bb: aa", output)
        self.assertIn("assent verify aa", output)
        self.assertIn("assent accept aa", output)
        self.assertIn("assent reconcile bb", output)
        self.assertIn("assent rework <PLAN> <TASK>", output)
        self.assertIn("assent reject bb", output)
        ask.assert_not_called()
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())
        self.assertNotEqual(old_receipt, b"")
        self.assertEqual(self.head(), target_tip)
        self.assertEqual(branch_tips, {
            branch: _git(self.root, "rev-parse", branch)
            for branch in branch_tips
        })

    def test_selected_target_conflict_points_to_reconcile_without_verifying(self) -> None:
        (self.root / "shared.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "shared base")
        self.make_source("aa")
        self.make_source("bb", filename="shared.txt", content="from bb\n")
        (self.root / "shared.txt").write_text("from trunk\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "advance trunk")

        target_tip = self.head()
        refs_before = {
            ref: self.head(ref)
            for ref in _git(self.root, "for-each-ref", "--format=%(refname)",
                            "refs/heads/").splitlines()
        }
        with mock.patch("assent.batch_verification.confirm_on_terminal") as ask, \
                mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_selected("aa", "bb")

        self.assertEqual(code, 1)
        self.assertIn("verify selected:", output)
        self.assertNotIn("verify --batch:", output)
        self.assertIn("full verifier did not run", output)
        self.assertIn("bb", output)
        self.assertIn("shared.txt", output)
        self.assertIn("bb conflicts with the integration target on its own", output)
        self.assertIn("assent reconcile bb", output)
        self.assertNotIn("compatible selected prefix", output)
        self.assertIn("no receipt was written", output)
        ask.assert_not_called()
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())
        self.assertEqual(self.head(), target_tip)
        self.assertEqual(refs_before, {
            ref: self.head(ref) for ref in refs_before
        })

    def test_selected_no_bisect_records_the_requested_set(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.write_verify(_VERIFY_FAILS)

        code, output = self.run_selected("bb", "aa", bisect=False)

        self.assertEqual(code, 1)
        self.assertNotIn("localiz", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertEqual(receipt.plan_names, ("aa", "bb"))

    def test_selected_bisection_prefix_cannot_authorize_original_set(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.make_source("cc")
        self.write_verify(
            "import pathlib\n"
            "import sys\n"
            "if pathlib.Path('cc.txt').exists():\n"
            "    print('regression introduced by cc')\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n")

        code, output = self.run_selected("cc", "aa", "bb")

        self.assertEqual(code, 1)
        self.assertIn("smaller PASSED prefix receipt does not authorize acceptance "
                      "of the originally requested full set", output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "bb"))


class TestRemainderSelection(BatchVerifyRepositoryCase):
    """``verify A ...`` resolves to exactly one verification of one exact set."""

    def run_cli(self, *plan_names: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = _dispatch(
                ["verify", *plan_names, "--config", str(self.config_path)])
        return code, output.getvalue()

    def test_remainder_writes_one_receipt_for_the_expanded_set(self) -> None:
        tips = {name: self.make_source(name) for name in ("aa", "bb", "cc")}
        self.write_task("ongoing", status="TODO")  # unfinished: not discovered

        with mock.patch("assent.__main__.verify_plan",
                        side_effect=AssertionError("ran the plan path too")):
            code, output = self.run_cli("cc", "...")

        self.assertEqual(code, 0, output)
        self.assertIn("verify: `...` selects cc, aa, bb", output)
        self.assertNotIn("ongoing", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.plan_names, ("aa", "bb", "cc"))
        self.assertEqual(
            [(source.plan, source.source_tip) for source in receipt.sources],
            [(name, tips[name]) for name in ("aa", "bb", "cc")])

    def test_remainder_conflict_is_refused_rather_than_skipped(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")

        with mock.patch("assent.batch_verification.confirm_on_terminal") as ask, \
                mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_cli("aa", "...")

        self.assertEqual(code, 1)
        self.assertNotIn("REVIEW UNRESOLVED, HUMAN DECISION", output)
        self.assertIn("candidate construction encountered merge conflicts", output)
        self.assertIn("complete exact selection remains required", output)
        ask.assert_not_called()
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())

    def test_a_one_plan_expansion_uses_the_ordinary_plan_path(self) -> None:
        self.make_source("aa")
        self.write_task("ongoing", status="TODO")

        with mock.patch("assent.__main__.verify_selected_batch",
                        side_effect=AssertionError("used the batch path")):
            code, output = self.run_cli("...")

        self.assertEqual(code, 0, output)
        self.assertFalse(self.receipt_path().exists())
        self.assertTrue(receipt_path(
            load_config(str(self.config_path), "aa")).exists())


class TestSkipConfirmation(unittest.TestCase):
    """The one interactive question in the whole batch path."""

    def test_only_a_clear_yes_is_a_yes_and_nothing_is_asked_twice(self) -> None:
        cases = (("", True), ("y", True), ("Y", True), (" yes ", True),
                 ("YES", True), ("n", False), ("no", False), ("N", False),
                 ("maybe", False), ("yy", False))
        for answer, expected in cases:
            with self.subTest(answer=answer), mock.patch(
                    "builtins.input", return_value=answer) as ask:
                self.assertIs(
                    confirm_on_terminal("Skip? [Y/n]: "), expected)
                ask.assert_called_once_with("Skip? [Y/n]: ")

    def test_a_closed_stdin_is_a_no(self) -> None:
        with mock.patch("builtins.input", side_effect=EOFError) as ask:
            self.assertFalse(confirm_on_terminal("Skip? [Y/n]: "))
        self.assertEqual(ask.call_count, 1)


class TestBatchConflictSkip(BatchVerifyRepositoryCase):
    """One human decision turns a conflicting batch into its independent subset."""

    def source_tips(self) -> dict[str, str]:
        return {branch: _git(self.root, "rev-parse", branch)
                for branch in _git(
                    self.root, "for-each-ref", "--format=%(refname:short)",
                    "refs/heads/").splitlines()}

    def test_a_conflict_free_batch_asks_nothing(self) -> None:
        self.make_source("aa")
        self.make_source("bb")

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.questions, [])
        self.assertNotIn("[Y/n]", output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "bb"))

    def test_yes_verifies_the_independent_subset_and_names_both_sets(self
                                                                     ) -> None:
        first = self.make_source("aa", filename="shared.txt",
                                 content="from aa\n")
        conflicting = self.make_source("bb", filename="shared.txt",
                                       content="from bb\n")
        third = self.make_source("cc")
        target_tip = self.head()

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.questions), 1)
        question = self.questions[0]
        self.assertTrue(question.endswith("[Y/n]: "), question)
        self.assertIn("Skip bb", question)
        self.assertIn("remaining 2 plan(s) (aa, cc)", question)
        self.assertIn("shared.txt", output)
        self.assertIn("verified aa, cc; skipped bb", output)

        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.plan_names, ("aa", "cc"))
        # The receipt records only positive facts about the verified subset,
        # and those trees are exactly what rebuilding that subset produces.
        rebuilt = build_batch_candidate(
            self.root, target_tip, [("aa", first), ("cc", third)])
        self.assertTrue(rebuilt.ok)
        self.assertEqual([s.step_tree for s in receipt.sources],
                         list(rebuilt.step_trees))
        self.assertEqual(self.head(), target_tip)

        # Strict rebuilding, which every freshness and acceptance check uses,
        # keeps refusing the same conflict instead of applying the skip.
        strict = build_batch_candidate(
            self.root, target_tip,
            [("aa", first), ("bb", conflicting), ("cc", third)])
        self.assertFalse(strict.ok)
        self.assertEqual(strict.conflict_plan, "bb")

    def test_the_queued_downstream_of_a_conflict_is_skipped_with_it(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")
        self.make_source("cc")
        self.make_source("dd")
        self.write_after("cc", ("bb",))

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 0, output)
        self.assertIn("cc is queued after bb", output)
        self.assertIn("Skip bb, cc", self.questions[0])
        self.assertIn("remaining 2 plan(s) (aa, dd)", self.questions[0])
        # A later independent plan is still attempted, so one scan sees every
        # conflict and the human is asked exactly once.
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "dd"))
        self.assertIn("verified aa, dd; skipped bb, cc", output)

    def test_several_conflicts_are_summarized_before_a_single_question(self
                                                                       ) -> None:
        self.make_source("aa", filename="one.txt", content="from aa\n")
        self.make_source("bb", filename="one.txt", content="from bb\n")
        self.make_source("cc", filename="two.txt", content="from cc\n")
        self.make_source("dd", filename="two.txt", content="from dd\n")

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 0, output)
        self.assertEqual(len(self.questions), 1)
        self.assertIn("Skip bb, dd", self.questions[0])
        self.assertIn("remaining 2 plan(s) (aa, cc)", self.questions[0])
        self.assertIn("merging bb into the batch candidate conflicts", output)
        self.assertIn("merging dd into the batch candidate conflicts", output)
        self.assertIn("one.txt", output)
        self.assertIn("two.txt", output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "cc"))

    def test_a_peer_only_conflict_is_not_presented_as_a_target_conflict(self
                                                                        ) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 0, output)
        self.assertIn("bb merges into the integration target cleanly on its "
                      "own", output)
        self.assertIn("never merges speculative peers", output)
        # Single-plan reconciliation cannot resolve a peer conflict, so it is
        # not offered, and the invalid one-argument rework is not either.
        self.assertNotIn("assent reconcile bb", output)
        self.assertIn("assent rework <PLAN> <TASK>", output)
        self.assertIn("assent reject bb", output)

    def test_a_conflict_with_the_target_itself_points_at_reconcile(self) -> None:
        (self.root / "shared.txt").write_text("base\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "shared base")
        self.make_source("aa")
        self.make_source("bb", filename="shared.txt", content="from bb\n")
        (self.root / "shared.txt").write_text("from trunk\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "advance trunk")

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 0, output)
        self.assertIn("bb conflicts with the integration target on its own",
                      output)
        self.assertIn("assent reconcile bb", output)
        self.assertNotIn("merges into the integration target cleanly", output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa",))

    def test_no_runs_no_verifier_writes_no_receipt_and_changes_nothing(self
                                                                      ) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        code, output = self.run_batch()
        self.assertEqual(code, 0, output)
        self.assertTrue(self.receipt_path().exists())

        self.make_source("bb", filename="shared.txt", content="from bb\n")
        target_tip = self.head()
        tips_before = self.source_tips()

        with mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_batch(answer=False)

        self.assertEqual(code, 1)
        verifier.assert_not_called()
        # The first, conflict-free run asked nothing at all.
        self.assertEqual(len(self.questions), 1)
        self.assertIn("the skip was declined", output)
        self.assertEqual(self.head(), target_tip)
        self.assertEqual(self.source_tips(), tips_before)
        # The earlier receipt was invalidated when this batch was attempted, so
        # a declined batch leaves no evidence behind that could still publish.
        self.assertFalse(self.receipt_path().exists())

    def test_the_default_confirmation_reads_stdin_and_eof_is_a_refusal(self
                                                                      ) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")

        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=EOFError) as ask, \
                mock.patch("assent.batch_verification.run_full_verifier") as verifier, \
                contextlib.redirect_stdout(output):
            code = verify_batch(str(self.config_path), self.assent_dir)

        self.assertEqual(code, 1, output.getvalue())
        self.assertTrue(ask.call_args.args[0].endswith("[Y/n]: "))
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())

    def test_an_all_conflicting_batch_asks_nothing_and_writes_no_receipt(self
                                                                        ) -> None:
        self.make_source("aa", filename="README.md", content="from aa\n")
        (self.root / "README.md").write_text("from trunk\n", encoding="utf-8")
        _git(self.root, "commit", "-am", "move the target")
        self.make_source("bb")
        self.write_after("bb", ("aa",))
        target_tip = self.head()

        with mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_batch(answer=True)

        self.assertEqual(code, 1)
        verifier.assert_not_called()
        self.assertEqual(self.questions, [])
        self.assertIn("README.md", output)
        self.assertIn("every queued plan conflicts", output)
        self.assertFalse(self.receipt_path().exists())
        self.assertEqual(self.head(), target_tip)

    def test_localization_operates_on_the_subset_that_was_verified(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")
        self.make_source("cc")
        self.write_verify(
            "import pathlib\n"
            "import sys\n"
            "if pathlib.Path('cc.txt').exists():\n"
            "    print('regression introduced by cc')\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n")

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 1)
        self.assertIn("localized the failure to cc", output)
        self.assertNotIn("localized the failure to bb", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.plan_names, ("aa",))

    def test_no_bisect_still_records_the_filtered_subset(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")
        self.make_source("cc")
        self.write_verify(_VERIFY_FAILS)

        code, output = self.run_batch(bisect=False, answer=True)

        self.assertEqual(code, 1)
        self.assertNotIn("localiz", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertEqual(receipt.plan_names, ("aa", "cc"))


class TestBatchFailureLocalization(BatchVerifyRepositoryCase):
    """Bisecting a failed batch down to the one plan that turns it red."""

    def setUp(self) -> None:
        super().setUp()
        self.run_log = self.parent / "verifier_runs.txt"

    def write_verify_red_on(self, plan_name: str) -> None:
        """Install a verifier that fails exactly when ``plan_name`` is merged in.

        Every run appends to a log outside the repository, so a test can also
        assert how many full verifications the localization actually spent.
        """
        self.write_verify(
            "import pathlib\n"
            "import sys\n"
            f"pathlib.Path({str(self.run_log)!r}).open('a').write('run\\n')\n"
            f"if pathlib.Path({plan_name + '.txt'!r}).exists():\n"
            f"    print('regression introduced by {plan_name}')\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n")

    def verifier_runs(self) -> int:
        if not self.run_log.exists():
            return 0
        return len(self.run_log.read_text(encoding="utf-8").splitlines())

    def make_batch(self, *plan_names: str) -> None:
        for plan_name in plan_names:
            self.make_source(plan_name)

    def test_guilty_plan_in_the_middle_is_named_and_the_prefix_is_kept(self
                                                                        ) -> None:
        self.make_batch("aa", "bb", "cc", "dd")
        self.write_verify_red_on("cc")
        guilty_task = (self.assent_dir / "cc" / "t001_task.e.toml")
        before = guilty_task.read_bytes()
        target_tip = self.head()

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("localized the failure to cc", output)
        self.assertIn("regression introduced by cc", output)
        # One failing full run, then ceil(log2(4)) localizing runs.
        self.assertEqual(self.verifier_runs(), 3)
        self.assertIn("at most 2 more full verification(s)", output)
        self.assertIn("localizing step 1/2", output)
        self.assertIn("localizing step 2/2", output)

        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.exit_code, 0)
        self.assertEqual(receipt.plan_names, ("aa", "bb"))
        self.assertIn("cc is the first plan", receipt.failure_summary)
        # The kept step trees come from a real verification of that prefix, so
        # rebuilding the same prefix must reproduce them exactly.
        rebuilt = build_batch_candidate(
            self.root, target_tip,
            [(s.plan, s.source_tip) for s in receipt.sources])
        self.assertTrue(rebuilt.ok)
        self.assertEqual([s.step_tree for s in receipt.sources],
                         list(rebuilt.step_trees))
        self.assertEqual(guilty_task.read_bytes(), before)
        self.assertEqual(self.head(), target_tip)

    def test_kept_prefix_receipt_is_published_by_accept_all(self) -> None:
        self.make_batch("aa", "bb", "cc")
        self.write_verify_red_on("cc")

        code, output = self.run_batch()
        self.assertEqual(code, 1, output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "bb"))

        published = io.StringIO()
        with contextlib.redirect_stdout(published):
            accepted = accept_all(str(self.config_path), self.assent_dir)

        self.assertEqual(accepted, 0, published.getvalue())
        self.assertIn("batch release done, 2 plan(s) published",
                      published.getvalue())
        self.assertTrue((self.root / "aa.txt").exists())
        self.assertTrue((self.root / "bb.txt").exists())
        self.assertFalse((self.root / "cc.txt").exists())
        self.assertFalse(self.receipt_path().exists())

    def test_guilty_first_plan_leaves_a_failed_receipt_and_no_prefix(self
                                                                      ) -> None:
        self.make_batch("aa", "bb", "cc")
        self.write_verify_red_on("aa")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("localized the failure to aa", output)
        self.assertIn("no plan ahead of it remains", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertNotEqual(receipt.exit_code, 0)
        self.assertEqual(receipt.plan_names, ("aa", "bb", "cc"))
        self.assertIn("aa is the first plan", receipt.failure_summary)

    def test_guilty_last_plan_keeps_every_earlier_plan(self) -> None:
        self.make_batch("aa", "bb", "cc", "dd")
        self.write_verify_red_on("dd")

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("localized the failure to dd", output)
        self.assertEqual(self.verifier_runs(), 3)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.plan_names, ("aa", "bb", "cc"))

    def test_a_single_plan_batch_needs_no_extra_verification(self) -> None:
        self.make_batch("aa")
        self.write_verify_red_on("aa")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertEqual(self.verifier_runs(), 1)
        self.assertIn("localized the failure to aa", output)
        self.assertEqual(self.read_batch_receipt().status, "FAILED")

    def test_downstream_of_the_guilty_plan_is_named_as_ejected(self) -> None:
        self.make_batch("aa", "bb", "cc")
        self.write_after("cc", ("bb",))
        self.write_verify_red_on("bb")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("bb and its downstream (cc) are out of this batch", output)
        self.assertIn("assent rework bb <TASK>", output)
        self.assertNotIn("`assent rework bb`", output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa",))

    def test_no_bisect_records_the_failure_without_localizing(self) -> None:
        self.make_batch("aa", "bb", "cc")
        self.write_verify_red_on("bb")

        code, output = self.run_batch(bisect=False)

        self.assertEqual(code, 1)
        self.assertEqual(self.verifier_runs(), 1)
        self.assertNotIn("localiz", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertEqual(receipt.exit_code, 3)
        self.assertEqual(receipt.plan_names, ("aa", "bb", "cc"))
        self.assertIn("regression introduced by bb", receipt.failure_summary)

    def test_a_conflict_is_still_refused_before_any_localization(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("merging bb into the batch candidate conflicts", output)
        self.assertNotIn("localiz", output)
        self.assertEqual(self.verifier_runs(), 0)
        self.assertFalse(self.receipt_path().exists())


class TestBatchLocking(BatchVerifyRepositoryCase):
    def test_a_busy_plan_lock_refuses_the_whole_batch(self) -> None:
        self.make_source("aa")
        self.make_source("bb")

        with hold_lock(self.assent_dir / "bb", "bb"):
            code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("verify --batch: refused", output)
        self.assertIn("bb", output)
        self.assertFalse(self.receipt_path().exists())

    def test_a_busy_integration_lock_refuses_the_whole_batch(self) -> None:
        self.make_source("aa")

        with hold_integration_lock(self.assent_dir):
            code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("verify --batch: refused", output)
        self.assertFalse(self.receipt_path().exists())


class TestBatchProvisionedLinks(BatchVerifyRepositoryCase):
    """A batch candidate gets the links of the worktrees it actually merges."""

    def link_target(self, name: str) -> Path:
        target = self.parent / f"external {name}"
        target.mkdir(exist_ok=True)
        (target / "marker.txt").write_text(f"{name} marker\n", encoding="utf-8")
        return target

    def provision(self, plan_name: str, name: str,
                  target: Path | None = None) -> Path:
        """Review a primary target, or install one deliberate foreign link."""
        worktree = gitops.worktree_path(self.root, plan_name)
        if target is not None:
            make_directory_link(worktree / name, target)
            return target
        target = self.root / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "marker.txt").write_text(
            f"{name} marker\n", encoding="utf-8")
        declared = set(getattr(self, "declared", ()))
        declared.add(name)
        self.declared = tuple(sorted(declared))
        shared_paths.declare(
            self.root, worktree, paths=self.declared, watch=("README.md",),
            dispositions=excluded_inventory(self.root, self.declared))
        return target

    def write_probe_verify(self, *probe: str, absent: tuple[str, ...] = (),
                           red_on: str = "") -> None:
        self.write_verify(
            "import sys\n"
            "from pathlib import Path\n"
            f"for name in {list(probe)!r}:\n"
            "    print('probe', name, (Path(name) / 'marker.txt')"
            ".read_text(encoding='utf-8').strip())\n"
            f"for name in {list(absent)!r}:\n"
            "    assert not Path(name).exists(), 'unexpected: ' + name\n"
            f"if {red_on!r} and Path({red_on!r}).exists():\n"
            f"    print('regression introduced by ' + {red_on!r})\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n")

    def test_dynamic_batch_mirrors_the_union_of_the_merged_worktrees(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        pkg = self.provision("aa", "pkg")
        assets = self.provision("bb", "assets")
        ordinary = gitops.worktree_path(self.root, "aa") / "ignored"
        ordinary.mkdir()
        self.write_probe_verify("pkg", "assets", absent=("ignored",))

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.read_batch_receipt().status, "PASSED")
        self.assertIn("Provisioned candidate link(s)", output)
        for plan_name, name, target in (("aa", "pkg", pkg),
                                     ("bb", "assets", assets)):
            source_link = gitops.worktree_path(self.root, plan_name) / name
            self.assertTrue((source_link / "marker.txt").is_file())
            self.assertTrue((target / "marker.txt").is_file())
        self.assertTrue(ordinary.is_dir())

    def test_dynamic_batch_mirrors_nested_links_and_generated_leaf_files(
            self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        arb = self.root / "lib/l10n/arb"
        arb.mkdir(parents=True)
        (arb / "app_localizations.dart").write_text("// l10n\n", encoding="utf-8")
        shared_paths.declare(
            self.root, gitops.worktree_path(self.root, "aa"),
            paths=("lib/l10n/arb",), watch=("README.md",),
            dispositions=excluded_inventory(self.root, ("lib/l10n/arb",)))
        part = gitops.worktree_path(self.root, "bb") / "lib/models/task.g.dart"
        part.write_text("// generated part\n", encoding="utf-8")
        cache = gitops.worktree_path(self.root, "bb") / "ignored"
        cache.mkdir()
        (cache / "build.g.dart").write_text("cached\n", encoding="utf-8")
        self.write_verify(
            "import sys\n"
            "from pathlib import Path\n"
            "for name in ['lib/l10n/arb/app_localizations.dart',\n"
            "             'lib/models/task.g.dart']:\n"
            "    print('read', name, Path(name).read_text(encoding='utf-8')"
            ".strip())\n"
            "assert not Path('ignored').exists(), 'ignored tree leaked'\n"
            "sys.exit(0)\n")

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.read_batch_receipt().status, "PASSED")
        # Both sources keep what they provisioned, and the target is untouched.
        self.assertTrue((arb / "app_localizations.dart").is_file())
        self.assertEqual(part.read_text(encoding="utf-8"), "// generated part\n")
        self.assertTrue((cache / "build.g.dart").is_file())

    def test_selected_batch_mirrors_the_links_of_the_named_plans(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.provision("aa", "pkg")
        self.write_probe_verify("pkg")

        code, output = self.run_selected("aa", "bb")

        self.assertEqual(code, 0, output)
        self.assertEqual(self.read_batch_receipt().plan_names, ("aa", "bb"))

    def test_localizing_a_failure_keeps_the_links_of_each_prefix(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.make_source("cc")
        self.provision("aa", "pkg")
        # Every prefix contains aa, so every localizing run must still be able
        # to read through the link, and cc is what turns the batch red.
        self.write_probe_verify("pkg", red_on="cc.txt")

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("localized the failure to cc", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.plan_names, ("aa", "bb"))

    def copy_ignored_package(self, plan_name: str) -> Path:
        """Give one source worktree a physical ignored pkg/fl_chart copy."""
        package = gitops.worktree_path(self.root, plan_name) / "pkg" / "fl_chart"
        package.mkdir(parents=True)
        (package / "pubspec.yaml").write_text("name: fl_chart\n",
                                              encoding="utf-8")
        return package

    def write_missing_package_verify(self) -> None:
        """Fail the way a dependency resolver does when pkg/fl_chart is gone."""
        self.write_verify(
            "import sys\n"
            "from pathlib import Path\n"
            "assert not Path('pkg').exists(), 'ignored tree leaked'\n"
            "print('Could not find a file named pubspec.yaml in "
            "pkg/fl_chart.', file=sys.stderr)\n"
            "sys.exit(1)\n")

    def test_a_selected_batch_diagnoses_a_copied_ignored_package(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.copy_ignored_package("aa")
        unrelated = gitops.worktree_path(self.root, "bb") / "ignored"
        unrelated.mkdir()
        self.write_missing_package_verify()

        code, output = self.run_selected("aa", "bb", bisect=False)

        self.assertEqual(code, 1, output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertIn("Could not find a file named pubspec.yaml",
                      receipt.failure_summary)
        self.assertIn("Ignored input diagnosis: pkg/", receipt.failure_summary)
        self.assertNotIn("ignored/", receipt.failure_summary)
        self.assertIn("verify selected: Ignored input diagnosis: pkg/", output)

    def test_a_dynamic_batch_diagnoses_a_copied_ignored_package(self) -> None:
        self.make_source("aa")
        self.copy_ignored_package("aa")
        self.write_missing_package_verify()

        code, output = self.run_batch(bisect=False)

        self.assertEqual(code, 1, output)
        self.assertIn("Ignored input diagnosis: pkg/",
                      self.read_batch_receipt().failure_summary)
        self.assertIn("verify --batch: Ignored input diagnosis: pkg/", output)

    def test_localizing_a_batch_keeps_the_ignored_input_diagnosis(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.copy_ignored_package("bb")
        # Only bb copied the ignored tree, so the prefix that first includes it
        # is where the diagnosis has to appear.
        self.write_verify(
            "import sys\n"
            "from pathlib import Path\n"
            "if Path('bb.txt').exists():\n"
            "    print('Could not find a file named pubspec.yaml in "
            "pkg/fl_chart.', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "sys.exit(0)\n")

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("localized the failure to bb", output)
        self.assertIn("Ignored input diagnosis: pkg/", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.plan_names, ("aa",))

    def test_conflicting_link_targets_refuse_the_whole_batch(self) -> None:
        self.make_source("aa")
        self.make_source("bb")
        self.provision("aa", "pkg")
        self.provision("bb", "pkg", self.link_target("other pkg"))
        self.write_probe_verify("pkg")

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("not a directory link to the reviewed primary target", output)
        self.assertIn("pkg", output)
        self.assertFalse(self.receipt_path().exists())

    def test_reviewed_none_refuses_an_external_link_before_batch_verify(self) -> None:
        self.make_source("aa")
        worktree = gitops.worktree_path(self.root, "aa")
        shared_paths.declare(
            self.root, worktree, none=True, watch=("README.md",),
            dispositions=excluded_inventory(self.root))
        target = self.link_target("pkg")
        make_directory_link(worktree / "pkg", target)

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("outside its active REVIEWED-NONE profile", output)
        self.assertNotIn("Full verification started", output)
        self.assertFalse(self.receipt_path().exists())
        self.assertTrue((target / "marker.txt").is_file())

    def test_an_occupied_candidate_destination_refuses_the_batch(self) -> None:
        self.make_source("aa")
        self.provision("aa", "pkg")
        # The target now tracks a real pkg/ directory, so the candidate owns
        # that name and a provisioned link may not take it over.
        (self.root / "pkg" / "keep.txt").write_text("tracked\n", encoding="utf-8")
        _git(self.root, "add", "-f", "pkg/keep.txt")
        _git(self.root, "commit", "-m", "track a real pkg directory")

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("no longer Git-ignored", output)
        self.assertFalse(self.receipt_path().exists())


if __name__ == "__main__":
    unittest.main()
