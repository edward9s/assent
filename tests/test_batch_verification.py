"""``assent verify --batch`` execution tests.

Which folders enter one candidate, in what order they are merged, what the
resulting batch receipt certifies, how a conflicting folder is skipped or
refused, and how a failed batch is localized to the single folder that breaks
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
from pathlib import Path
from unittest import mock

from assent import gitops
from assent.__main__ import _dispatch
from assent.batch_accept import accept_all
from assent.batch_receipt import (BatchVerificationReceipt, batch_receipt_path,
                                  read_batch_receipt)
from assent.batch_verification import (confirm_on_terminal, verify_batch,
                                       verify_selected_batch)
from assent.config import load_config
from assent.folder_verification import receipt_path
from assent.lockfile import hold_integration_lock, hold_lock
from assent.verification_common import build_batch_candidate

_VERIFY_OK = "raise SystemExit(0)\n"
_VERIFY_FAILS = "print('two tests failed')\nraise SystemExit(3)\n"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class BatchVerifyRepositoryCase(unittest.TestCase):
    """A trunk repository plus helpers for building finished source folders."""

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
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")

        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
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

    def write_task(self, folder: str, status: str = "DONE") -> Path:
        tasks_dir = self.assent_dir / folder
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / "t001_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "core"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            'verify = "python .assent/verify.py"\n'
            'goal = "Complete the task."\n'
            'acceptance = "Verification passes."\n',
            encoding="utf-8")
        return path

    def write_after(self, folder: str, after: tuple[str, ...]) -> None:
        values = ", ".join(f'"{item}"' for item in after)
        (self.assent_dir / folder / "_folder.toml").write_text(
            f"after = [{values}]\n", encoding="utf-8")

    def make_source(self, folder: str, *, filename: str | None = None,
                    content: str = "result\n", status: str = "DONE") -> str:
        """Create a finished folder with one commit on its own source branch."""
        self.write_task(folder, status=status)
        worktree = gitops.ensure_worktree(self.root, folder)
        branch = gitops.ensure_branch(worktree, f"{folder}/")
        (worktree / (filename or f"{folder}.txt")).write_text(
            content, encoding="utf-8")
        gitops.commit_all(worktree, f"finish {folder}")
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

    def run_selected(self, *folders: str, bisect: bool = True
                     ) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_selected_batch(
                str(self.config_path), self.assent_dir, folders, bisect)
        return code, output.getvalue()


class TestBatchSelection(BatchVerifyRepositoryCase):
    def test_no_folder_at_all_is_an_empty_batch_with_no_receipt(self) -> None:
        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertIn("no folder has anything left to verify", output)
        self.assertFalse(self.receipt_path().exists())

    def test_unfinished_and_source_less_folders_are_skipped_not_failed(self
                                                                      ) -> None:
        for status in ("TODO", "WIP", "BLOCKED"):
            self.write_task(f"folder-{status.lower()}", status=status)
        self.write_task("cleaned")  # DONE, but no branch and no worktree

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        for status in ("TODO", "WIP", "BLOCKED"):
            self.assertIn(f"skip folder-{status.lower()}", output)
        self.assertIn("skip cleaned (no source branch remains", output)
        self.assertIn("no folder has anything left to verify", output)
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
        # Lexicographically the folders are alpha, mike, zulu; the declared
        # dependency must push alpha behind zulu.
        self.make_source("alpha")
        self.make_source("mike")
        self.make_source("zulu")
        self.write_after("alpha", ("zulu",))

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(self.read_batch_receipt().folders,
                         ("mike", "zulu", "alpha"))
        self.assertIn("mike, zulu, alpha", output)

    def test_ordering_is_stable_across_repeated_runs(self) -> None:
        for folder in ("delta", "bravo", "charlie"):
            self.make_source(folder)
        self.write_after("bravo", ("delta",))

        orders = []
        for _ in range(2):
            code, output = self.run_batch()
            self.assertEqual(code, 0, output)
            orders.append(self.read_batch_receipt().folders)

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
        self.assertEqual([(s.folder, s.source_tip) for s in receipt.sources],
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

    def test_conflicting_folder_is_named_and_no_receipt_is_written(self) -> None:
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
        # authorize a release of the folders it used to cover.
        self.assertFalse(self.receipt_path().exists())

    def test_batch_leaves_single_folder_receipts_untouched(self) -> None:
        self.make_source("aa")
        cfg = load_config(str(self.config_path), "aa")
        folder_receipt = receipt_path(cfg)
        folder_receipt.write_text("placeholder\n", encoding="utf-8")

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(folder_receipt.read_text(encoding="utf-8"),
                         "placeholder\n")


class TestExplicitBatchSelection(BatchVerifyRepositoryCase):
    def test_selected_names_are_normalized_and_receipt_is_exact(self) -> None:
        parent = self.make_source("parent")
        child = self.make_source("child")
        self.write_after("child", ("parent",))

        target_tip = self.head()
        code, output = self.run_selected("child", "parent")

        self.assertEqual(code, 0, output)
        self.assertIn("merging 2 folder(s) in dependency order: parent, child",
                      output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.folders, ("parent", "child"))
        self.assertEqual(
            [(source.folder, source.source_tip) for source in receipt.sources],
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
        self.assertIn("exact selected set conflicts", output)
        self.assertIn("shared.txt", output)
        ask.assert_not_called()
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())
        self.assertNotEqual(old_receipt, b"")
        self.assertEqual(self.head(), target_tip)
        self.assertEqual(branch_tips, {
            branch: _git(self.root, "rev-parse", branch)
            for branch in branch_tips
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
        self.assertEqual(receipt.folders, ("aa", "bb"))

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
        self.assertEqual(self.read_batch_receipt().folders, ("aa", "bb"))


class TestRemainderSelection(BatchVerifyRepositoryCase):
    """``verify A ...`` resolves to exactly one verification of one exact set."""

    def run_cli(self, *folders: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = _dispatch(
                ["verify", *folders, "--config", str(self.config_path)])
        return code, output.getvalue()

    def test_remainder_writes_one_receipt_for_the_expanded_set(self) -> None:
        tips = {name: self.make_source(name) for name in ("aa", "bb", "cc")}
        self.write_task("ongoing", status="TODO")  # unfinished: not discovered

        with mock.patch("assent.__main__.verify_folder",
                        side_effect=AssertionError("ran the folder path too")):
            code, output = self.run_cli("cc", "...")

        self.assertEqual(code, 0, output)
        self.assertIn("verify: `...` selects cc, aa, bb", output)
        self.assertNotIn("ongoing", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.folders, ("aa", "bb", "cc"))
        self.assertEqual(
            [(source.folder, source.source_tip) for source in receipt.sources],
            [(name, tips[name]) for name in ("aa", "bb", "cc")])

    def test_remainder_conflict_is_refused_rather_than_skipped(self) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")

        with mock.patch("assent.batch_verification.confirm_on_terminal") as ask, \
                mock.patch("assent.batch_verification.run_full_verifier") as verifier:
            code, output = self.run_cli("aa", "...")

        self.assertEqual(code, 1)
        self.assertIn("exact selected set conflicts", output)
        ask.assert_not_called()
        verifier.assert_not_called()
        self.assertFalse(self.receipt_path().exists())

    def test_a_one_folder_expansion_uses_the_ordinary_folder_path(self) -> None:
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
        self.assertEqual(self.read_batch_receipt().folders, ("aa", "bb"))

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
        self.assertIn("remaining 2 folder(s) (aa, cc)", question)
        self.assertIn("shared.txt", output)
        self.assertIn("verified aa, cc; skipped bb", output)

        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.folders, ("aa", "cc"))
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
        self.assertEqual(strict.conflict_folder, "bb")

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
        self.assertIn("remaining 2 folder(s) (aa, dd)", self.questions[0])
        # A later independent folder is still attempted, so one scan sees every
        # conflict and the human is asked exactly once.
        self.assertEqual(self.read_batch_receipt().folders, ("aa", "dd"))
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
        self.assertIn("remaining 2 folder(s) (aa, cc)", self.questions[0])
        self.assertIn("merging bb into the batch candidate conflicts", output)
        self.assertIn("merging dd into the batch candidate conflicts", output)
        self.assertIn("one.txt", output)
        self.assertIn("two.txt", output)
        self.assertEqual(self.read_batch_receipt().folders, ("aa", "cc"))

    def test_a_peer_only_conflict_is_not_presented_as_a_target_conflict(self
                                                                        ) -> None:
        self.make_source("aa", filename="shared.txt", content="from aa\n")
        self.make_source("bb", filename="shared.txt", content="from bb\n")

        code, output = self.run_batch(answer=True)

        self.assertEqual(code, 0, output)
        self.assertIn("bb merges into the integration target cleanly on its "
                      "own", output)
        self.assertIn("never merges speculative peers", output)
        # Single-folder reconciliation cannot resolve a peer conflict, so it is
        # not offered, and the invalid one-argument rework is not either.
        self.assertNotIn("assent reconcile bb", output)
        self.assertIn("assent rework <FOLDER> <TASK>", output)
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
        self.assertEqual(self.read_batch_receipt().folders, ("aa",))

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
        self.assertIn("every queued folder conflicts", output)
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
        self.assertEqual(receipt.folders, ("aa",))

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
        self.assertEqual(receipt.folders, ("aa", "cc"))


class TestBatchFailureLocalization(BatchVerifyRepositoryCase):
    """Bisecting a failed batch down to the one folder that turns it red."""

    def setUp(self) -> None:
        super().setUp()
        self.run_log = self.parent / "verifier_runs.txt"

    def write_verify_red_on(self, folder: str) -> None:
        """Install a verifier that fails exactly when ``folder`` is merged in.

        Every run appends to a log outside the repository, so a test can also
        assert how many full verifications the localization actually spent.
        """
        self.write_verify(
            "import pathlib\n"
            "import sys\n"
            f"pathlib.Path({str(self.run_log)!r}).open('a').write('run\\n')\n"
            f"if pathlib.Path({folder + '.txt'!r}).exists():\n"
            f"    print('regression introduced by {folder}')\n"
            "    sys.exit(3)\n"
            "sys.exit(0)\n")

    def verifier_runs(self) -> int:
        if not self.run_log.exists():
            return 0
        return len(self.run_log.read_text(encoding="utf-8").splitlines())

    def make_batch(self, *folders: str) -> None:
        for folder in folders:
            self.make_source(folder)

    def test_guilty_folder_in_the_middle_is_named_and_the_prefix_is_kept(self
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
        self.assertEqual(receipt.folders, ("aa", "bb"))
        self.assertIn("cc is the first folder", receipt.failure_summary)
        # The kept step trees come from a real verification of that prefix, so
        # rebuilding the same prefix must reproduce them exactly.
        rebuilt = build_batch_candidate(
            self.root, target_tip,
            [(s.folder, s.source_tip) for s in receipt.sources])
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
        self.assertEqual(self.read_batch_receipt().folders, ("aa", "bb"))

        published = io.StringIO()
        with contextlib.redirect_stdout(published):
            accepted = accept_all(str(self.config_path), self.assent_dir)

        self.assertEqual(accepted, 0, published.getvalue())
        self.assertIn("batch release done, 2 folder(s) published",
                      published.getvalue())
        self.assertTrue((self.root / "aa.txt").exists())
        self.assertTrue((self.root / "bb.txt").exists())
        self.assertFalse((self.root / "cc.txt").exists())
        self.assertFalse(self.receipt_path().exists())

    def test_guilty_first_folder_leaves_a_failed_receipt_and_no_prefix(self
                                                                      ) -> None:
        self.make_batch("aa", "bb", "cc")
        self.write_verify_red_on("aa")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("localized the failure to aa", output)
        self.assertIn("no folder ahead of it remains", output)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "FAILED")
        self.assertNotEqual(receipt.exit_code, 0)
        self.assertEqual(receipt.folders, ("aa", "bb", "cc"))
        self.assertIn("aa is the first folder", receipt.failure_summary)

    def test_guilty_last_folder_keeps_every_earlier_folder(self) -> None:
        self.make_batch("aa", "bb", "cc", "dd")
        self.write_verify_red_on("dd")

        code, output = self.run_batch()

        self.assertEqual(code, 1, output)
        self.assertIn("localized the failure to dd", output)
        self.assertEqual(self.verifier_runs(), 3)
        receipt = self.read_batch_receipt()
        self.assertEqual(receipt.status, "PASSED")
        self.assertEqual(receipt.folders, ("aa", "bb", "cc"))

    def test_a_single_folder_batch_needs_no_extra_verification(self) -> None:
        self.make_batch("aa")
        self.write_verify_red_on("aa")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertEqual(self.verifier_runs(), 1)
        self.assertIn("localized the failure to aa", output)
        self.assertEqual(self.read_batch_receipt().status, "FAILED")

    def test_downstream_of_the_guilty_folder_is_named_as_ejected(self) -> None:
        self.make_batch("aa", "bb", "cc")
        self.write_after("cc", ("bb",))
        self.write_verify_red_on("bb")

        code, output = self.run_batch()

        self.assertEqual(code, 1)
        self.assertIn("bb and its downstream (cc) are out of this batch", output)
        self.assertIn("assent rework bb", output)
        self.assertEqual(self.read_batch_receipt().folders, ("aa",))

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
        self.assertEqual(receipt.folders, ("aa", "bb", "cc"))
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
    def test_a_busy_folder_lock_refuses_the_whole_batch(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
