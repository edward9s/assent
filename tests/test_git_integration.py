"""Tests for the local accept Git integration foundation."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from assent import AssentError
from assent.gitops import (
    AcceptStatus,
    accept_commit_message,
    branch_tip,
    build_accept_trailers,
    commit_of,
    find_accept_evidence,
    folder_branches,
    folder_worktree,
    is_ancestor,
    main_worktree,
    parse_accept_evidence,
    require_current_branch,
    temporary_integration_worktree,
    temporary_source_worktree,
    unique_folder_branch,
    working_tree_status,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True,
        encoding="utf-8", errors="replace", check=True)
    return result.stdout.strip()


class GitRepositoryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = Path(tempfile.mkdtemp(prefix="assent accept 測試 "))
        self.root = self.parent / "repository with spaces"
        self.root.mkdir()
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Assent Test")
        _git(self.root, "config", "user.email", "assent@example.invalid")
        _git(self.root, "checkout", "-b", "trunk")
        (self.root / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "initial")
        self.initial = commit_of(self.root, "HEAD")
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.root.exists():
            output = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=self.root,
                capture_output=True, encoding="utf-8", errors="replace")
            for line in output.stdout.splitlines():
                if not line.startswith("worktree "):
                    continue
                path = Path(line.removeprefix("worktree "))
                if path.resolve() != self.root.resolve():
                    subprocess.run(
                        ["git", "worktree", "remove", "--force", str(path)],
                        cwd=self.root, capture_output=True)
        shutil.rmtree(self.parent, ignore_errors=True)

    def _source(self, branch: str = "plan01/run") -> str:
        _git(self.root, "checkout", "-b", branch)
        (self.root / "result.txt").write_text(branch, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "source result")
        tip = commit_of(self.root, "HEAD")
        _git(self.root, "checkout", "trunk")
        return tip

    def _merge_with_message(self, tip: str, message: str) -> None:
        _git(self.root, "merge", "--no-ff", "-m", message, tip)


class TestRepositoryFacts(GitRepositoryCase):
    def test_main_worktree_from_main_and_linked_worktree(self) -> None:
        self.assertEqual(main_worktree(self.root), self.root.resolve())
        linked = self.parent / "linked 工作樹"
        _git(self.root, "worktree", "add", "--detach", str(linked), self.initial)
        self.assertEqual(main_worktree(linked), self.root.resolve())

    def test_current_branch_and_detached_target(self) -> None:
        self.assertEqual(require_current_branch(self.root), "trunk")
        _git(self.root, "checkout", "--detach", self.initial)
        with self.assertRaisesRegex(AssentError, "detached HEAD"):
            require_current_branch(self.root)

    def test_status_categorizes_all_three_kinds(self) -> None:
        (self.root / "staged.txt").write_text("staged", encoding="utf-8")
        _git(self.root, "add", "staged.txt")
        (self.root / "README.md").write_text("unstaged\n", encoding="utf-8")
        (self.root / "untracked 名稱.txt").write_text("new", encoding="utf-8")
        status = working_tree_status(self.root)
        self.assertEqual(status.staged, ["staged.txt"])
        self.assertEqual(status.unstaged, ["README.md"])
        self.assertEqual(status.untracked, ["untracked 名稱.txt"])
        self.assertFalse(status.is_clean)

    def test_status_excludes_runtime_artifact(self) -> None:
        (self.root / "_assent.log").write_text("runtime", encoding="utf-8")
        self.assertTrue(
            working_tree_status(self.root, ("_assent.log",)).is_clean)

    def test_folder_worktree_is_resolved_from_a_linked_worktree(self) -> None:
        fixed = self.parent / f"{self.root.name}.worktrees" / "plan01"
        fixed.parent.mkdir()
        _git(self.root, "worktree", "add", "--detach", str(fixed), self.initial)
        self.assertEqual(folder_worktree(fixed, "plan01"), fixed.resolve())
        self.assertIsNone(folder_worktree(self.root, "missing"))

    def test_unique_and_multiple_folder_branches(self) -> None:
        self.assertIsNone(unique_folder_branch(self.root, "plan01"))
        _git(self.root, "branch", "plan01/one", self.initial)
        self.assertEqual(unique_folder_branch(self.root, "plan01"), "plan01/one")
        _git(self.root, "branch", "plan01/two", self.initial)
        _git(self.root, "branch", "plan02/other", self.initial)
        self.assertEqual(
            folder_branches(self.root, "plan01"), ["plan01/one", "plan01/two"])
        with self.assertRaisesRegex(AssentError, "multiple local branches"):
            unique_folder_branch(self.root, "plan01")

    def test_branch_tip_and_ancestor(self) -> None:
        tip = self._source()
        self.assertEqual(branch_tip(self.root, "plan01/run"), tip)
        self.assertTrue(is_ancestor(self.root, self.initial, tip))
        self.assertFalse(is_ancestor(self.root, tip, self.initial))

    def test_git_errors_keep_exit_code_and_summary(self) -> None:
        with self.assertRaises(AssentError) as caught:
            branch_tip(self.root, "missing-branch")
        message = str(caught.exception)
        self.assertIn("exit code", message)
        self.assertIn("missing-branch", message)
        with self.assertRaisesRegex(AssentError, "exit code"):
            is_ancestor(self.root, "missing-commit", "HEAD")


class TestAcceptEvidence(GitRepositoryCase):
    def test_build_and_parse_evidence(self) -> None:
        text = build_accept_trailers("plan01", "plan01/run", "a" * 40)
        evidence = parse_accept_evidence(f"accept plan01\n\n{text}\n")
        self.assertEqual(evidence.folder, "plan01")
        self.assertEqual(evidence.source_branch, "plan01/run")
        self.assertEqual(evidence.source_tip, "a" * 40)

    def test_creation_rejects_empty_controls_and_wrong_branch(self) -> None:
        bad_values = (
            ("", "plan01/run", "a" * 40),
            ("plan01\nAssent-Source-Tip: " + "b" * 40,
             "plan01/run", "a" * 40),
            ("plan01", "plan01/run\rmalicious", "a" * 40),
            ("plan01", "other/run", "a" * 40),
            ("plan01", "plan01/run", "short"),
        )
        for values in bad_values:
            with self.subTest(values=values), self.assertRaises(AssentError):
                build_accept_trailers(*values)
        with self.assertRaises(AssentError):
            accept_commit_message("subject\nbody", "plan01", "plan01/run", "a" * 40)

    def test_parser_rejects_incomplete_duplicate_and_other_folder_branch(self) -> None:
        incomplete = "Assent-Folder: plan01\nAssent-Source-Branch: plan01/run\n"
        duplicate = (build_accept_trailers("plan01", "plan01/run", "a" * 40)
                     + "\nAssent-Folder: plan01\n")
        other = ("Assent-Folder: plan01\nAssent-Source-Branch: other/run\n"
                 f"Assent-Source-Tip: {'a' * 40}\n")
        self.assertIsNone(parse_accept_evidence(incomplete))
        self.assertIsNone(parse_accept_evidence(duplicate))
        self.assertIsNone(parse_accept_evidence(other))

    def test_real_two_parent_merge_is_accepted_after_branch_deletion(self) -> None:
        tip = self._source()
        message = accept_commit_message(
            "accept: integrate plan01", "plan01", "plan01/run", tip)
        self._merge_with_message(tip, message)
        _git(self.root, "branch", "-D", "plan01/run")
        result = find_accept_evidence(self.root, "plan01")
        self.assertEqual(result.status, AcceptStatus.ACCEPTED)
        self.assertEqual(result.evidence.source_tip, tip)

    def test_plain_commit_with_forged_trailers_is_uncertain(self) -> None:
        tip = self._source()
        (self.root / "note.txt").write_text("not a merge", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", accept_commit_message(
            "document trailers", "plan01", "plan01/run", tip))
        self.assertEqual(
            find_accept_evidence(self.root, "plan01").status,
            AcceptStatus.UNCERTAIN)

    def test_wrong_second_parent_is_uncertain(self) -> None:
        tip = self._source()
        message = accept_commit_message(
            "false parent", "plan01", "plan01/run", self.initial)
        self._merge_with_message(tip, message)
        self.assertEqual(
            find_accept_evidence(self.root, "plan01").status,
            AcceptStatus.UNCERTAIN)

    def test_other_folder_branch_and_nonexistent_tip_are_uncertain(self) -> None:
        tip = self._source("other/run")
        other_message = (
            "merge\n\nAssent-Folder: plan01\nAssent-Source-Branch: other/run\n"
            f"Assent-Source-Tip: {tip}\n")
        self._merge_with_message(tip, other_message)
        self.assertEqual(
            find_accept_evidence(self.root, "plan01").status,
            AcceptStatus.UNCERTAIN)

        _git(self.root, "reset", "--hard", self.initial)
        (self.root / "fake.txt").write_text("fake", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", accept_commit_message(
            "fake tip", "plan01", "plan01/run", "0" * 40))
        self.assertEqual(
            find_accept_evidence(self.root, "plan01").status,
            AcceptStatus.UNCERTAIN)

    def test_absent_when_history_has_no_folder_evidence(self) -> None:
        self.assertEqual(
            find_accept_evidence(self.root, "plan01").status,
            AcceptStatus.ABSENT)


class TestTemporaryWorktrees(GitRepositoryCase):
    def _metadata_paths(self) -> list[Path]:
        return [Path(line.removeprefix("worktree ")).resolve()
                for line in _git(self.root, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def _temporary_entries(self) -> list[Path]:
        container = self.parent / f"{self.root.name}.integration"
        return list(container.iterdir()) if container.exists() else []

    def test_source_uses_explicit_snapshot_and_cleans_success(self) -> None:
        (self.root / "later.txt").write_text("later", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "later")
        with temporary_source_worktree(self.root, self.initial) as path:
            self.assertEqual(commit_of(path, "HEAD"), self.initial)
            self.assertFalse((path / "later.txt").exists())
            self.assertIn(path.resolve(), self._metadata_paths())
        self.assertFalse(path.exists())
        self.assertEqual(self._temporary_entries(), [])
        self.assertEqual(self._metadata_paths(), [self.root.resolve()])

    def test_source_cleans_after_python_exception(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "verify failure"):
            with temporary_source_worktree(self.root, self.initial) as path:
                (path / "generated.txt").write_text("dirty", encoding="utf-8")
                raise RuntimeError("verify failure")
        self.assertFalse(path.exists())
        self.assertEqual(self._temporary_entries(), [])

    def test_integration_uses_snapshot_and_cleans_success(self) -> None:
        (self.root / "later.txt").write_text("later", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "later")
        with temporary_integration_worktree(
                self.root, "plan01", self.initial) as (path, branch):
            self.assertEqual(commit_of(path, "HEAD"), self.initial)
            self.assertEqual(require_current_branch(path), branch)
            self.assertFalse((path / "later.txt").exists())
        self.assertFalse(path.exists())
        self.assertEqual(self._temporary_entries(), [])
        self.assertNotIn(branch, folder_branches(self.root, "assent-integration"))
        self.assertEqual(self._metadata_paths(), [self.root.resolve()])
        self.assertTrue(working_tree_status(self.root).is_clean)

    def test_integration_cleans_dirty_conflict_or_failure_state(self) -> None:
        _git(self.root, "checkout", "-b", "plan01/conflicting", self.initial)
        (self.root / "README.md").write_text("source side\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "source conflict")
        source_tip = commit_of(self.root, "HEAD")
        _git(self.root, "checkout", "trunk")
        (self.root / "README.md").write_text("target side\n", encoding="utf-8")
        _git(self.root, "add", "README.md")
        _git(self.root, "commit", "-m", "target conflict")
        target_tip = commit_of(self.root, "HEAD")

        with temporary_integration_worktree(
                self.root, "plan01", target_tip) as (path, branch):
            merge = subprocess.run(
                ["git", "merge", "--no-ff", source_tip], cwd=path,
                capture_output=True, encoding="utf-8", errors="replace")
            self.assertNotEqual(merge.returncode, 0)
            self.assertTrue((path / ".git").is_file())
        self.assertFalse(path.exists())
        self.assertEqual(self._temporary_entries(), [])
        self.assertNotIn(branch, _git(
            self.root, "for-each-ref", "--format=%(refname:short)",
            "refs/heads/").splitlines())
        self.assertEqual(self._metadata_paths(), [self.root.resolve()])


if __name__ == "__main__":
    unittest.main()
