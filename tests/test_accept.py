"""End-to-end regressions for receipt-gated transactional acceptance."""
from __future__ import annotations

import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from assent import AssentError, gitops, verification
from assent import accept as accept_mod
from assent.accept import accept_folder
from assent.config import load_config
from assent.lockfile import hold_integration_lock, hold_lock

_DEFAULT_VERIFY = "python .assent/verify.py"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        errors="replace", check=True)
    return result.stdout.strip()


class AcceptRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent accept test "))
        self.root = self.parent / "repository with spaces"
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

        self.folder = "plan01"
        self.assent_dir = self.root / ".assent"
        self.tasks_dir = self.assent_dir / self.folder
        self.tasks_dir.mkdir(parents=True)
        self.config_path = self.assent_dir / "assent.toml"
        self.config_path.write_text("", encoding="utf-8")
        (self.assent_dir / "verify.py").write_text(
            "raise SystemExit('accept must not run the full verifier')\n",
            encoding="utf-8")

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

    def _write_task(self, task_id: str = "t001", status: str = "DONE", *,
                    folder: str | None = None,
                    verify: str = _DEFAULT_VERIFY) -> Path:
        tasks_dir = self.assent_dir / (folder or self.folder)
        tasks_dir.mkdir(parents=True, exist_ok=True)
        path = tasks_dir / f"{task_id}_task.e.toml"
        path.write_text(
            'title = "Task"\n'
            'deps = []\n'
            'model = "prime"\n'
            f'status = "{status}"\n'
            'scope = ["assent/"]\n'
            f'verify = {verify!r}\n'
            'goal = "Complete the task."\n'
            'acceptance = "Verification passes."\n',
            encoding="utf-8")
        return path

    def _make_source(self, folder: str | None = None, *,
                     filename: str | None = None, content: str = "result\n"
                     ) -> tuple[Path, str, str]:
        folder = folder or self.folder
        filename = filename or f"{folder}-result.txt"
        worktree = gitops.ensure_worktree(self.root, folder)
        branch = gitops.ensure_branch(worktree, f"{folder}/")
        (worktree / filename).write_text(content, encoding="utf-8")
        gitops.commit_all(worktree, f"finish {folder}")
        return worktree, branch, gitops.branch_tip(self.root, branch)

    def _config(self, folder: str | None = None):
        return load_config(self.config_path, folder or self.folder)

    def _accept(self, folder: str | None = None) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = accept_folder(self._config(folder))
        return code, output.getvalue()

    def _head(self, ref: str = "HEAD") -> str:
        return _git(self.root, "rev-parse", ref)

    def _write_receipt(
            self, folder: str | None = None, *, status: str = "PASSED",
            integration_tree: str | None = None,
            assert_exact: bool = True) -> verification.VerificationReceipt:
        folder = folder or self.folder
        cfg = self._config(folder)
        target_tip = self._head()
        branches = gitops.folder_branches(self.root, folder)
        self.assertEqual(len(branches), 1)
        source_tip = gitops.branch_tip(self.root, branches[0])
        with gitops.temporary_integration_worktree(
                self.root, folder, target_tip) as (candidate, _branch):
            outcome = gitops.merge_no_ff(
                candidate, source_tip, f"prepare receipt for {folder}")
            self.assertTrue(outcome.ok, outcome.conflicts)
            self.assertEqual(
                gitops.commit_parents(candidate), (target_tip, source_tip))
            reconstructed_tree = gitops.tree_of(candidate, "HEAD")
        digest = verification.verifier_digest(cfg)
        receipt = verification.VerificationReceipt(
            version=verification.RECEIPT_VERSION,
            status=status,
            source_tip=source_tip,
            target_tip=target_tip,
            integration_tree=integration_tree or reconstructed_tree,
            verify_script_sha256=digest,
            verify_command=verification.VERIFY_COMMAND,
            exit_code=0 if status == "PASSED" else 7,
            completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            failure_summary="" if status == "PASSED" else "simulated failure",
        )
        verification.write_receipt(
            verification.receipt_path(cfg), receipt, self.root)
        stored = verification.read_receipt(
            verification.receipt_path(cfg), self.root)
        self.assertEqual(stored.source_tip, source_tip)
        self.assertEqual(stored.verify_script_sha256, digest)
        if assert_exact:
            self.assertEqual(stored.integration_tree, reconstructed_tree)
        return stored

    def _assert_no_temporary_state(self) -> None:
        container = self.parent / f"{self.root.name}.integration"
        self.assertFalse(container.exists() and list(container.iterdir()))
        self.assertEqual(
            gitops.branches_with_prefix(self.root, "assent-integration/"), [])

    def _assert_refused_unchanged(self, before: str,
                                  result: tuple[int, str]) -> str:
        code, output = result
        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), before)
        self._assert_no_temporary_state()
        return output


class TestAcceptSuccess(AcceptRepositoryCase):
    def test_exact_receipt_publishes_ff_only_merge_without_running_verifiers(
            self) -> None:
        self._write_task("t002", "SKIP", verify="must-not-run-second")
        self._write_task("t001", "DONE", verify="must-not-run-first")
        worktree, branch, source_tip = self._make_source()
        before = self._head()
        receipt = self._write_receipt()

        with patch.object(
                verification, "_run_full_verifier",
                side_effect=AssertionError("accept ran the full verifier")) as verifier:
            with patch.object(
                    gitops, "fast_forward", wraps=gitops.fast_forward) as publish:
                code, output = self._accept()

        self.assertEqual(code, 0, output)
        verifier.assert_not_called()
        publish.assert_called_once()
        after = self._head()
        self.assertEqual(
            gitops.commit_parents(self.root, after), (before, source_tip))
        self.assertEqual(gitops.tree_of(self.root, after), receipt.integration_tree)
        message = gitops.commit_message(self.root, after)
        for value in (
                f"Assent-Folder: {self.folder}",
                f"Assent-Source-Branch: {branch}",
                f"Assent-Source-Tip: {source_tip}",
                f"Assent-Verified-Tree: {receipt.integration_tree}",
                f"Assent-Verifier-SHA256: {receipt.verify_script_sha256}"):
            self.assertIn(value, message)
        self.assertTrue(worktree.exists())
        self.assertIn(branch, gitops.folder_branches(self.root, self.folder))
        self.assertIn("without running verification", output)
        self.assertIn("retain it while a dependent may still need its source evidence",
                      output)
        self.assertIn("clean plan01", output)
        self._assert_no_temporary_state()

        code, output = self._accept()
        self.assertEqual(code, 0, output)
        self.assertEqual(self._head(), after)
        self.assertIn("already accepted", output)

    def test_unique_branch_without_worktree_uses_receipt_backed_snapshot(self) -> None:
        self._write_task()
        worktree, branch, source_tip = self._make_source()
        receipt = self._write_receipt()
        gitops.remove_worktree(self.root, worktree)

        code, output = self._accept()

        self.assertEqual(code, 0, output)
        self.assertEqual(self._head("HEAD^2"), source_tip)
        self.assertEqual(gitops.tree_of(self.root, "HEAD"), receipt.integration_tree)
        self.assertIn(branch, gitops.folder_branches(self.root, self.folder))
        self._assert_no_temporary_state()

    def test_cleaned_source_is_not_reauthorized_by_audit_trailers(self) -> None:
        self._write_task()
        worktree, branch, _source_tip = self._make_source()
        self._write_receipt()
        self.assertEqual(self._accept()[0], 0)
        gitops.remove_worktree(self.root, worktree)
        gitops.delete_branch_force(self.root, branch)
        accepted = self._head()

        code, output = self._accept()

        self.assertEqual(code, 1, output)
        self.assertEqual(self._head(), accepted)
        self.assertIn("no source worktree", output)
        self.assertIn("does not infer authorization", output)


class TestAcceptPrechecks(AcceptRepositoryCase):
    def test_unfinished_statuses_refuse_but_done_and_skip_are_complete(self) -> None:
        for status in ("TODO", "WIP", "BLOCKED"):
            with self.subTest(status=status):
                shutil.rmtree(self.tasks_dir, ignore_errors=True)
                self._write_task(status=status)
                before = self._head()
                code, output = self._accept()
                self.assertEqual(code, 1, output)
                self.assertIn("not finished", output)
                self.assertEqual(self._head(), before)

        shutil.rmtree(self.tasks_dir, ignore_errors=True)
        self._write_task("t001", "DONE")
        self._write_task("t002", "SKIP")
        self._make_source()
        self._write_receipt()
        code, output = self._accept()
        self.assertEqual(code, 0, output)

    def test_lock_order_busy_refusal_and_release(self) -> None:
        self._write_task()
        self._make_source()
        self._write_receipt()
        before = self._head()
        events: list[str] = []
        original_integration = accept_mod.hold_integration_lock
        original_folder = accept_mod.hold_lock

        @contextlib.contextmanager
        def traced_integration(path):
            events.append("enter integration")
            with original_integration(path):
                yield
            events.append("exit integration")

        @contextlib.contextmanager
        def traced_folder(path, folder):
            events.append("enter folder")
            with original_folder(path, folder):
                yield
            events.append("exit folder")

        with patch.object(accept_mod, "hold_integration_lock", traced_integration), \
                patch.object(accept_mod, "hold_lock", traced_folder):
            code, output = self._accept()
        self.assertEqual(code, 0, output)
        self.assertEqual(events, [
            "enter integration", "enter folder", "exit folder", "exit integration"])

        with hold_lock(self.tasks_dir, self.folder):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("already processing", output)
        with hold_integration_lock(self.assent_dir):
            code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("integration is already running", output)
        self.assertNotEqual(self._head(), before)

        with hold_integration_lock(self.assent_dir):
            with hold_lock(self.tasks_dir, self.folder):
                pass

    def test_target_must_be_clean_and_attached(self) -> None:
        self._write_task()
        self._make_source()
        self._write_receipt()
        before = self._head()
        (self.root / "dirty.txt").write_text("dirty", encoding="utf-8")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("main worktree", output)

        (self.root / "dirty.txt").unlink()
        _git(self.root, "checkout", "--detach", "HEAD")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("detached HEAD", output)

    def test_source_must_be_clean_attached_and_on_folder_branch(self) -> None:
        self._write_task()
        worktree, _branch, _tip = self._make_source()
        self._write_receipt()
        before = self._head()

        (worktree / "dirty.txt").write_text("dirty", encoding="utf-8")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("source worktree", output)
        (worktree / "dirty.txt").unlink()

        gitops.remove_worktree(self.root, worktree)
        worktree = gitops.ensure_worktree(self.root, self.folder)
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("detached HEAD", output)

        gitops.remove_worktree(self.root, worktree)
        worktree = gitops.ensure_worktree(self.root, self.folder)
        _git(worktree, "checkout", "-b", "foreign/run")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("not a plan01/* branch", output)

    def test_absent_and_ambiguous_source_fail_closed(self) -> None:
        self._write_task()
        before = self._head()
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("no source worktree", output)
        self.assertIn("does not infer authorization", output)

        _git(self.root, "branch", f"{self.folder}/one", "HEAD")
        _git(self.root, "branch", f"{self.folder}/two", "HEAD")
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("multiple candidate", output)
        self.assertEqual(self._head(), before)

    def test_bad_selected_plan_or_folder_graph_fails_closed(self) -> None:
        before = self._head()
        malformed = self.tasks_dir / "t001_task.e.toml"
        malformed.write_text("not valid = [\n", encoding="utf-8")
        code, _output = self._accept()
        self.assertEqual(code, 1)
        self.assertEqual(self._head(), before)

        malformed.unlink()
        self._write_task()
        (self.tasks_dir / "_folder.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")
        code, output = self._accept()
        self.assertEqual(code, 1, output)
        self.assertIn("does not exist", output)
        self.assertEqual(self._head(), before)


class TestReceiptRefusals(AcceptRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        self._write_task()
        self.worktree, self.branch, self.source_tip = self._make_source()

    def test_missing_malformed_and_failed_receipts_change_no_target(self) -> None:
        before = self._head()
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("receipt not found", output.lower())

        receipt_path = self.tasks_dir / verification.RECEIPT_NAME
        receipt_path.write_text("not valid = [\n", encoding="utf-8")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("not valid TOML", output)

        self._write_receipt(status="FAILED")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("status is FAILED", output)

    def test_source_tip_and_verifier_digest_staleness_fail_closed(self) -> None:
        self._write_receipt()
        before = self._head()
        (self.worktree / "later.txt").write_text("later\n", encoding="utf-8")
        gitops.commit_all(self.worktree, "advance source")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("source tip changed", output)

        self._write_receipt()
        with (self.assent_dir / "verify.py").open("a", encoding="utf-8") as handle:
            handle.write("# digest changed\n")
        output = self._assert_refused_unchanged(before, self._accept())
        self.assertIn("verification script changed", output)

    def test_reconstructed_tree_mismatch_fails_before_publication(self) -> None:
        target_tree = gitops.tree_of(self.root, "HEAD")
        self._write_receipt(
            integration_tree=target_tree, assert_exact=False)
        before = self._head()

        output = self._assert_refused_unchanged(before, self._accept())

        self.assertIn("candidate tree differs", output)


class TestDependencyGate(AcceptRepositoryCase):
    def _dependent(self, name: str, upstream: str) -> None:
        self._write_task(folder=name)
        (self.assent_dir / name / "_folder.toml").write_text(
            f'after = ["{upstream}"]\n', encoding="utf-8")
        self._make_source(name)

    def test_only_current_upstream_ancestry_authorizes_dependency(self) -> None:
        base = "base"
        self._write_task(folder=base)
        _base_worktree, base_branch, base_tip = self._make_source(base)
        _git(self.root, "merge", "--no-ff", "-m", "accept base manually", base_branch)
        self.assertTrue(gitops.is_ancestor(self.root, base_tip, self._head()))

        unrelated = self.assent_dir / "unrelated"
        unrelated.mkdir()
        (unrelated / "t001_bad.e.toml").write_text(
            "not valid = [\n", encoding="utf-8")
        self._dependent(self.folder, base)
        receipt = self._write_receipt()

        code, output = self._accept()

        self.assertEqual(code, 0, output)
        self.assertEqual(gitops.tree_of(self.root, "HEAD"), receipt.integration_tree)

    def test_upstream_advance_missing_and_ambiguity_all_fail_closed(self) -> None:
        base = "base"
        self._write_task(folder=base)
        base_worktree, base_branch, _base_tip = self._make_source(base)
        _git(self.root, "merge", "--no-ff", "-m", "accept base manually", base_branch)
        (base_worktree / "later.txt").write_text("later\n", encoding="utf-8")
        gitops.commit_all(base_worktree, "advance base")
        self._dependent("after-advance", base)
        before = self._head()

        code, output = self._accept("after-advance")
        self.assertEqual(code, 1, output)
        self.assertIn("current tip", output)
        self.assertIn("not in target", output)
        self.assertEqual(self._head(), before)

        gitops.remove_worktree(self.root, base_worktree)
        gitops.delete_branch_force(self.root, base_branch)
        self._dependent("after-clean", base)
        code, output = self._accept("after-clean")
        self.assertEqual(code, 1, output)
        self.assertIn("source was cleaned", output)
        self.assertEqual(self._head(), before)

        _git(self.root, "branch", "base/one", "HEAD")
        _git(self.root, "branch", "base/two", "HEAD")
        self._dependent("after-ambiguous", base)
        code, output = self._accept("after-ambiguous")
        self.assertEqual(code, 1, output)
        self.assertIn("current source is ambiguous", output)
        self.assertEqual(self._head(), before)


class TestAcceptTransactionalFailures(AcceptRepositoryCase):
    def setUp(self) -> None:
        super().setUp()
        self._write_task()
        self.worktree, self.branch, self.source_tip = self._make_source()
        self._write_receipt()

    def _assert_preserved(self, before: str) -> None:
        self.assertEqual(self._head("trunk"), before)
        self.assertTrue(self.worktree.exists())
        self.assertIn(self.branch, gitops.folder_branches(self.root, self.folder))
        self._assert_no_temporary_state()

    def _race_after_candidate_tree(self, action) -> tuple[int, str]:
        original_tree_of = gitops.tree_of
        fired = False

        def tree_then_race(root: Path, committish: str) -> str:
            nonlocal fired
            tree = original_tree_of(root, committish)
            if not fired and Path(root).resolve() != self.root.resolve():
                fired = True
                action()
            return tree

        with patch.object(gitops, "tree_of", side_effect=tree_then_race):
            result = self._accept()
        self.assertTrue(fired)
        return result

    def test_merge_conflict_lists_paths_and_preserves_state(self) -> None:
        (self.worktree / "README.md").write_text("source\n", encoding="utf-8")
        gitops.commit_all(self.worktree, "source conflict")
        self._write_receipt()
        (self.root / "README.md").write_text("target\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "target conflict")
        before = self._head()

        code, output = self._accept()

        self.assertEqual(code, 1, output)
        self.assertIn("Conflicting file(s)", output)
        self.assertIn("README.md", output)
        self._assert_preserved(before)

    def test_final_target_head_move_is_not_overwritten(self) -> None:
        before = self._head()

        def move_target() -> None:
            (self.root / "concurrent.txt").write_text("move", encoding="utf-8")
            _git(self.root, "add", "concurrent.txt")
            _git(self.root, "commit", "-m", "concurrent target move")

        code, output = self._race_after_candidate_tree(move_target)

        self.assertEqual(code, 1, output)
        self.assertIn("moved during accept", output)
        self.assertNotEqual(self._head(), before)
        self._assert_no_temporary_state()

    def test_final_target_branch_switch_never_advances_wrong_ref(self) -> None:
        before = self._head()
        _git(self.root, "branch", "other", before)

        code, output = self._race_after_candidate_tree(
            lambda: _git(self.root, "switch", "other"))

        self.assertEqual(code, 1, output)
        self.assertIn("no longer on trunk", output)
        self.assertEqual(self._head("trunk"), before)
        self.assertEqual(self._head("other"), before)
        self._assert_no_temporary_state()

    def test_final_target_cleanliness_race_refuses_publication(self) -> None:
        before = self._head()

        def dirty_target() -> None:
            (self.root / "concurrent.txt").write_text("dirty", encoding="utf-8")

        code, output = self._race_after_candidate_tree(dirty_target)

        self.assertEqual(code, 1, output)
        self.assertIn("became dirty", output)
        self._assert_preserved(before)

    def test_final_source_race_refuses_publication(self) -> None:
        before = self._head()

        def advance_source() -> None:
            (self.worktree / "concurrent.txt").write_text("source", encoding="utf-8")
            gitops.commit_all(self.worktree, "concurrent source move")

        code, output = self._race_after_candidate_tree(advance_source)

        self.assertEqual(code, 1, output)
        self.assertIn("source changed during acceptance", output)
        self._assert_preserved(before)

    def test_cleanup_diagnostic_after_publication_is_warning_not_failure(self) -> None:
        before = self._head()
        original_cleanup = gitops._cleanup_temporary_worktree

        def cleanup_then_report(*args, **kwargs) -> None:
            original_cleanup(*args, **kwargs)
            raise AssentError("simulated cleanup diagnostic")

        output = io.StringIO()
        with patch.object(
                gitops, "_cleanup_temporary_worktree",
                side_effect=cleanup_then_report):
            with contextlib.redirect_stdout(output):
                code = accept_folder(self._config())

        self.assertEqual(code, 0, output.getvalue())
        self.assertNotEqual(self._head(), before)
        self.assertIn("warning: simulated cleanup diagnostic", output.getvalue())
        self.assertIn("temporary ref:", output.getvalue())
        self.assertIn("temporary path:", output.getvalue())
        self.assertTrue(self.worktree.exists())
        self._assert_no_temporary_state()


if __name__ == "__main__":
    unittest.main()
