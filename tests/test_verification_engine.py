"""Scheduler handoff tests for unattended folder verification.

The second half of this module covers ``assent verify --batch``: which folders
enter one candidate, in what order they are merged, what the resulting batch
receipt certifies, and how a failed batch is localized to the single folder that
breaks it.  Those tests run against disposable local repositories rather than
mocks, because the facts under test are Git facts.
"""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from assent import engine, gitops, verification
from assent.accept import accept_all
from assent.config import load_config
from assent.lockfile import hold_integration_lock, hold_lock
from assent.verification import (
    VerificationReceipt, _run_full_verifier, _verify_locked,
    verify_batch, verify_folder_if_needed,
)


def _task(status: str) -> str:
    return (
        'title = "Task"\n'
        'deps = []\n'
        'model = "lite"\n'
        f'status = "{status}"\n'
        'scope = ["src/"]\n'
        'verify = "python -m unittest tests.test_main"\n'
        'goal = "Finish"\n'
        'acceptance = "Focused tests pass"\n'
    )


class VerificationEngineCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="assent verification engine "))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / "work"
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        self.task_path = self.tasks_dir / "t001_task.e.toml"
        self.cfg = load_config(self.config_path, "work")

    def write_status(self, status: str) -> None:
        self.task_path.write_text(_task(status), encoding="utf-8")


class TestRunVerificationHandoff(VerificationEngineCase):
    def _run_with_body(self, body, verify_result=0, **options):
        events: list[str] = []

        @contextmanager
        def folder_lock(*_args):
            events.append("folder-enter")
            yield
            events.append("folder-exit")

        def verify(_cfg):
            events.append("verify")
            return verify_result

        with mock.patch("assent.engine.lockfile.hold_lock", folder_lock), \
                mock.patch("assent.engine._run_locked", side_effect=body), \
                mock.patch("assent.engine.verification.verify_folder_if_needed",
                           side_effect=verify), \
                mock.patch("assent.engine._try_write_report"):
            with contextlib.redirect_stdout(io.StringIO()):
                result = engine.run(self.cfg, **options)
        return result, events

    def test_last_task_triggers_after_session_folder_lock_is_released(self):
        self.write_status("TODO")

        def body(*_args):
            self.write_status("DONE")
            return 0

        result, events = self._run_with_body(body, once=True)
        self.assertEqual(result, 0)
        self.assertEqual(events, ["folder-enter", "folder-exit", "verify"])

    def test_once_task_and_normal_runs_verify_only_when_folder_is_complete(self):
        for options in ({}, {"once": True}, {"task_id": "t001"}):
            with self.subTest(options=options):
                self.write_status("TODO")

                def body(*_args):
                    self.write_status("DONE")
                    return 0

                result, events = self._run_with_body(body, **options)
                self.assertEqual(result, 0)
                self.assertEqual(events[-1], "verify")

        for status in ("TODO", "WIP", "BLOCKED"):
            with self.subTest(status=status):
                self.write_status(status)
                result, events = self._run_with_body(lambda *_args: 0)
                self.assertEqual(result, 0)
                self.assertNotIn("verify", events)

    def test_full_verification_failure_is_folder_level_and_nonzero(self):
        self.write_status("DONE")
        result, events = self._run_with_body(lambda *_args: 0, verify_result=1)
        self.assertEqual(result, 1)
        self.assertEqual(events.count("verify"), 1)
        self.assertIn('status = "DONE"', self.task_path.read_text(encoding="utf-8"))

    def test_task_run_failure_never_starts_full_verification(self):
        self.write_status("DONE")
        result, events = self._run_with_body(lambda *_args: 1)
        self.assertEqual(result, 1)
        self.assertNotIn("verify", events)


class TestAutomaticReceiptPolicy(VerificationEngineCase):
    def test_lock_order_is_integration_then_folder_and_fresh_pass_skips_suite(self):
        self.write_status("DONE")
        events: list[str] = []

        @contextmanager
        def integration_lock(_path):
            events.append("integration-enter")
            yield
            events.append("integration-exit")

        @contextmanager
        def folder_lock(*_args):
            events.append("folder-enter")
            yield
            events.append("folder-exit")

        receipt = VerificationReceipt(
            version=1, status="PASSED", source_tip="a" * 40,
            target_tip="b" * 40, integration_tree="c" * 40,
            verify_script_sha256="d" * 64,
            verify_command="python .assent/verify.py", exit_code=0,
            completed_at="2026-07-22T00:00:00+00:00", failure_summary="")
        (self.tasks_dir / "_verification.toml").write_text(
            "placeholder\n", encoding="utf-8")

        with mock.patch("assent.verification.hold_integration_lock",
                        integration_lock), \
                mock.patch("assent.verification.hold_lock", folder_lock), \
                mock.patch(
                    "assent.verification._receipt_matches_current_candidate_locked",
                    return_value=True), \
                mock.patch("assent.verification.read_receipt", return_value=receipt), \
                mock.patch("assent.verification.gitops.main_worktree",
                           return_value=self.root), \
                mock.patch("assent.verification._verify_locked") as full:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify_folder_if_needed(self.cfg)

        self.assertEqual(result, 0)
        self.assertEqual(events, [
            "integration-enter", "folder-enter", "folder-exit", "integration-exit"])
        full.assert_not_called()

    def test_invalid_existing_receipt_fails_closed_without_full_suite(self):
        self.write_status("DONE")
        (self.tasks_dir / "_verification.toml").write_text(
            "not valid = [", encoding="utf-8")
        with mock.patch(
                "assent.verification._receipt_matches_current_candidate_locked",
                side_effect=engine.AssentError("bad receipt")), \
                mock.patch("assent.verification._verify_locked") as full:
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify_folder_if_needed(self.cfg)
        self.assertEqual(result, 1)
        full.assert_not_called()

    def test_explicit_refresh_preserves_invalid_receipt_and_starts_no_candidate(self):
        self.write_status("DONE")
        path = self.tasks_dir / "_verification.toml"
        invalid = "not valid = ["
        path.write_text(invalid, encoding="utf-8")

        with mock.patch(
                "assent.verification.gitops.temporary_integration_worktree") as candidate:
            with self.assertRaises(engine.AssentError):
                _verify_locked(self.cfg)

        self.assertEqual(path.read_text(encoding="utf-8"), invalid)
        candidate.assert_not_called()


class TestVerificationPrompt(VerificationEngineCase):
    def test_timeout_is_not_defined_as_sufficient_for_blocked(self):
        self.write_status("TODO")
        task = engine.Plan.parse(self.tasks_dir).tasks[0]
        session = engine._SessionIdentity(
            agent="codex", requested_model="model", effort="high",
            requested_effort="high")
        prompt = engine._build_prompt(self.cfg, task, None, session)
        self.assertIn("focused task gate", prompt)
        self.assertIn("do not start a concurrent duplicate", prompt)
        self.assertIn("do not mark the task BLOCKED solely", prompt)
        self.assertIn("scheduler runs the\nsame command", prompt)


class TestFullVerifierProcess(unittest.TestCase):
    def test_slow_verifier_has_no_timeout_and_reports_elapsed_and_exit_code(self):
        completed = subprocess.CompletedProcess(["verifier"], 7, "out", "err")
        output = io.StringIO()
        with mock.patch("assent.verification.subprocess.run",
                        return_value=completed) as run_child, \
                mock.patch("assent.verification.time.monotonic",
                           side_effect=[10.0, 311.25]), \
                contextlib.redirect_stdout(output):
            actual = _run_full_verifier(
                Path("verify.py"), Path("candidate with spaces"))

        self.assertIs(actual, completed)
        self.assertNotIn("timeout", run_child.call_args.kwargs)
        self.assertEqual(run_child.call_args.kwargs["cwd"],
                         "candidate with spaces")
        self.assertIn("Full verification started", output.getvalue())
        self.assertIn("elapsed 301.2s, exit code 7", output.getvalue())

    def test_interrupt_is_reported_and_propagated_for_candidate_cleanup(self):
        output = io.StringIO()
        with mock.patch("assent.verification.subprocess.run",
                        side_effect=KeyboardInterrupt), \
                mock.patch("assent.verification.time.monotonic",
                           side_effect=[20.0, 325.0]), \
                contextlib.redirect_stdout(output), \
                self.assertRaises(KeyboardInterrupt):
            _run_full_verifier(Path("verify.py"), Path("candidate"))

        self.assertIn("elapsed 305.0s, exit code 130", output.getvalue())


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
        return verification.batch_receipt_path(self.assent_dir)

    def read_batch_receipt(self) -> verification.BatchVerificationReceipt:
        return verification.read_batch_receipt(self.receipt_path(), self.root)

    def run_batch(self) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_batch(str(self.config_path), self.assent_dir)
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
        rebuilt = verification.build_batch_candidate(
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
        folder_receipt = verification.receipt_path(cfg)
        folder_receipt.write_text("placeholder\n", encoding="utf-8")

        code, output = self.run_batch()

        self.assertEqual(code, 0, output)
        self.assertEqual(folder_receipt.read_text(encoding="utf-8"),
                         "placeholder\n")


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

    def run_batch(self, bisect: bool = True) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = verify_batch(str(self.config_path), self.assent_dir, bisect)
        return code, output.getvalue()

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
        rebuilt = verification.build_batch_candidate(
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
