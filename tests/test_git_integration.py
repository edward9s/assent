"""Tests for the local accept Git integration foundation."""
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent import gitops, pathops
from assent.gitops import (
    accept_commit_message,
    branch_tip,
    build_accept_trailers,
    commit_parents,
    commit_of,
    plan_branches,
    plan_worktree,
    is_ancestor,
    main_worktree,
    merge_no_ff,
    object_type,
    require_current_branch,
    temporary_integration_worktree,
    tree_of,
    unique_plan_branch,
    working_tree_status,
)
from assent.verification_common import (ProvisionedLink,
                                        provisioned_candidate_links)
from tests.link_support import cleanup_worktree, make_directory_link, safe_rmtree


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
                    cleanup_worktree(self.root, path)
        safe_rmtree(self.parent)

    def _source(self, branch: str = "plan01/run") -> str:
        _git(self.root, "checkout", "-b", branch)
        (self.root / "result.txt").write_text(branch, encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "source result")
        tip = commit_of(self.root, "HEAD")
        _git(self.root, "checkout", "trunk")
        return tip

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

    def test_plan_worktree_is_resolved_from_a_linked_worktree(self) -> None:
        fixed = self.parent / f"{self.root.name}.worktrees" / "plan01"
        fixed.parent.mkdir()
        _git(self.root, "worktree", "add", "--detach", str(fixed), self.initial)
        self.assertEqual(plan_worktree(fixed, "plan01"), fixed.resolve())
        self.assertIsNone(plan_worktree(self.root, "missing"))

    def test_unique_and_multiple_plan_branches(self) -> None:
        self.assertIsNone(unique_plan_branch(self.root, "plan01"))
        _git(self.root, "branch", "plan01/one", self.initial)
        self.assertEqual(unique_plan_branch(self.root, "plan01"), "plan01/one")
        _git(self.root, "branch", "plan01/two", self.initial)
        _git(self.root, "branch", "plan02/other", self.initial)
        self.assertEqual(
            plan_branches(self.root, "plan01"), ["plan01/one", "plan01/two"])
        with self.assertRaisesRegex(AssentError, "multiple local branches"):
            unique_plan_branch(self.root, "plan01")

    def test_branch_tip_and_ancestor(self) -> None:
        tip = self._source()
        self.assertEqual(branch_tip(self.root, "plan01/run"), tip)
        self.assertTrue(is_ancestor(self.root, self.initial, tip))
        self.assertFalse(is_ancestor(self.root, tip, self.initial))

    def test_is_path_ignored_answers_for_absent_directory_and_tracked_paths(
            self) -> None:
        (self.root / ".gitignore").write_text(
            "pkg/\ncache\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "ignore rules")
        # A directory-only rule needs the directory form to match a name that
        # does not exist yet, which is exactly the provisioning question.
        self.assertFalse(gitops.is_path_ignored(self.root, "pkg"))
        self.assertTrue(gitops.is_path_ignored(self.root, "pkg", directory=True))
        self.assertTrue(gitops.is_path_ignored(self.root, "cache", directory=True))
        self.assertFalse(gitops.is_path_ignored(self.root, "src", directory=True))
        # A tracked path is never reported as ignored.
        self.assertFalse(
            gitops.is_path_ignored(self.root, "README.md", directory=True))

    def test_git_errors_keep_exit_code_and_summary(self) -> None:
        with self.assertRaises(AssentError) as caught:
            branch_tip(self.root, "missing-branch")
        message = str(caught.exception)
        self.assertIn("exit code", message)
        self.assertIn("missing-branch", message)
        with self.assertRaisesRegex(AssentError, "exit code"):
            is_ancestor(self.root, "missing-commit", "HEAD")


class TestPassiveAcceptMetadata(GitRepositoryCase):
    def test_builder_emits_readable_passive_metadata_for_both_object_id_sizes(self) -> None:
        text = build_accept_trailers(
            "plan01", "plan01/run", "a" * 40, "b" * 64, "c" * 64)
        self.assertEqual(text.splitlines(), [
            "Assent-Plan: plan01",
            "Assent-Source-Branch: plan01/run",
            f"Assent-Source-Tip: {'a' * 40}",
            f"Assent-Verified-Tree: {'b' * 64}",
            f"Assent-Verifier-SHA256: {'c' * 64}",
        ])

    def test_builder_rejects_empty_controls_and_invalid_values(self) -> None:
        good_values = ("plan01", "plan01/run", "a" * 40, "b" * 40, "c" * 64)
        bad_values = (
            ("", *good_values[1:]),
            ("plan01\nAssent-Source-Tip: " + "b" * 40, *good_values[1:]),
            ("plan01", "plan01/run\rmalicious", *good_values[2:]),
            (*good_values[:2], "a" * 40 + "\t", *good_values[3:]),
            (*good_values[:3], "short", good_values[4]),
            (*good_values[:4], "short"),
            ("plan01", "other/run", *good_values[2:]),
        )
        for values in bad_values:
            with self.subTest(values=values), self.assertRaises(AssentError):
                build_accept_trailers(*values)
        with self.assertRaises(AssentError):
            accept_commit_message("subject\nbody", *good_values)

    def test_no_ff_merge_keeps_parents_and_tree_with_passive_metadata(self) -> None:
        source_tip = self._source()
        source_tree = tree_of(self.root, source_tip)
        message = accept_commit_message(
            "accept: integrate plan01", "plan01", "plan01/run", source_tip,
            source_tree, "d" * 64)

        outcome = merge_no_ff(self.root, source_tip, message)

        merge_tip = commit_of(self.root, "HEAD")
        self.assertTrue(outcome.ok)
        self.assertEqual(commit_parents(self.root), (self.initial, source_tip))
        self.assertEqual(tree_of(self.root, merge_tip), source_tree)
        self.assertEqual(object_type(self.root, merge_tip), "commit")


class TestTemporaryWorktrees(GitRepositoryCase):
    def _metadata_paths(self) -> list[Path]:
        return [Path(line.removeprefix("worktree ")).resolve()
                for line in _git(self.root, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def _temporary_entries(self) -> list[Path]:
        container = self.parent / f"{self.root.name}.integration"
        return list(container.iterdir()) if container.exists() else []

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
        self.assertNotIn(branch, plan_branches(self.root, "assent-integration"))
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

    def test_detachment_failure_retains_temporary_branch_until_retry(self) -> None:
        (self.root / ".gitignore").write_text("pkg/\n", encoding="utf-8")
        _git(self.root, "add", ".gitignore")
        _git(self.root, "commit", "-m", "ignore temporary link")
        snapshot = commit_of(self.root, "HEAD")
        target = self.parent / "temporary target"
        target.mkdir()
        (target / "sentinel.txt").write_text("keep\n", encoding="utf-8")

        with mock.patch.object(
                pathops, "detach_directory_link",
                side_effect=OSError("simulated detachment refusal")):
            with self.assertRaisesRegex(AssentError, "cleanup was incomplete"):
                with temporary_integration_worktree(
                        self.root, "plan01", snapshot) as (path, branch):
                    make_directory_link(path / "pkg", target)

        self.assertTrue(path.exists())
        self.assertIn(branch, _git(
            self.root, "for-each-ref", "--format=%(refname:short)",
            "refs/heads/").splitlines())
        self.assertTrue((target / "sentinel.txt").is_file())

        gitops._cleanup_temporary_worktree(self.root, path, branch)

        self.assertFalse(path.exists())
        self.assertNotIn(branch, _git(
            self.root, "for-each-ref", "--format=%(refname:short)",
            "refs/heads/").splitlines())
        self.assertTrue((target / "sentinel.txt").is_file())

    def test_a_mirrored_link_is_removed_before_the_candidate_worktree(self) -> None:
        """Candidate cleanup must never reach an external linked target.

        ``git worktree remove --force`` walks into a junction and deletes what
        it finds, so the mirror has to be gone before cleanup starts.  Nesting
        the link context inside the worktree context is what guarantees that,
        and this proves the external target survives the whole sequence.
        """
        (self.root / ".gitignore").write_text("pkg/\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "ignore pkg")
        snapshot = commit_of(self.root, "HEAD")
        target = self.parent / "external pkg"
        target.mkdir()
        (target / "marker.txt").write_text("keep\n", encoding="utf-8")

        with temporary_integration_worktree(
                self.root, "plan01", snapshot) as (path, _branch):
            with provisioned_candidate_links(
                    path, (ProvisionedLink("pkg", target),)) as mirrored:
                self.assertEqual([link.path for link in mirrored], ["pkg"])
                self.assertTrue((path / "pkg" / "marker.txt").is_file())
            self.assertFalse((path / "pkg").exists())

        self.assertFalse(path.exists())
        self.assertTrue((target / "marker.txt").is_file())
        self.assertEqual(self._temporary_entries(), [])

    def test_a_mirrored_link_survives_neither_a_failure_nor_an_interruption(
            self) -> None:
        (self.root / ".gitignore").write_text("pkg/\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "ignore pkg")
        snapshot = commit_of(self.root, "HEAD")
        target = self.parent / "external pkg"
        target.mkdir()
        (target / "marker.txt").write_text("keep\n", encoding="utf-8")

        for failure in (RuntimeError("verifier blew up"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with self.assertRaises(type(failure)):
                    with temporary_integration_worktree(
                            self.root, "plan01", snapshot) as (path, _branch):
                        with provisioned_candidate_links(
                                path, (ProvisionedLink("pkg", target),)):
                            raise failure
                self.assertFalse(path.exists())
                self.assertTrue((target / "marker.txt").is_file())
                self.assertEqual(self._temporary_entries(), [])

    def test_cleanup_diagnostic_does_not_replace_primary_exception(self) -> None:
        original_cleanup = gitops._cleanup_temporary_worktree

        def cleanup_then_report(*args, **kwargs):
            original_cleanup(*args, **kwargs)
            raise AssentError("simulated cleanup diagnostic")

        with mock.patch.object(
                gitops, "_cleanup_temporary_worktree",
                side_effect=cleanup_then_report):
            with self.assertRaisesRegex(RuntimeError, "primary failure") as caught:
                with temporary_integration_worktree(
                        self.root, "plan01", self.initial):
                    raise RuntimeError("primary failure")
        self.assertTrue(any(
            "simulated cleanup diagnostic" in note
            for note in getattr(caught.exception, "__notes__", ())))
        self.assertEqual(self._temporary_entries(), [])


class TestNonTraversingWorktreeRemoval(GitRepositoryCase):
    """Removing a worktree must detach its directory links, never walk them.

    With Git 2.46.0.windows.1 a plain ``git worktree remove`` reports success
    after walking an ignored junction and deleting the sentinel on the other
    side, and ``shutil.rmtree`` is no safer, so every removal path empties the
    worktree of link objects first.  Everything here uses a real junction on
    Windows and a real directory symlink elsewhere, at the worktree root and
    nested below a tracked directory.
    """

    def setUp(self) -> None:
        super().setUp()
        (self.root / ".gitignore").write_text(
            "pkg/\nlib/build/\ngenerated/\n", encoding="utf-8")
        (self.root / "lib").mkdir()
        (self.root / "lib" / "app.py").write_text("tracked\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "ignore the provisioned link paths")
        self.snapshot = commit_of(self.root, "HEAD")

    def _external(self, name: str) -> Path:
        """An external directory holding sentinels at two depths."""
        target = self.parent / f"external {name}"
        (target / "nested").mkdir(parents=True)
        (target / "sentinel.txt").write_text(f"{name}\n", encoding="utf-8")
        (target / "nested" / "deep.txt").write_text(
            f"{name} deep\n", encoding="utf-8")
        return target

    def _contents(self, *targets: Path) -> list[tuple[str, str]]:
        """Every filename and content digest below the given targets."""
        return sorted(
            (str(path.relative_to(target.parent)).replace("\\", "/"),
             hashlib.sha256(path.read_bytes()).hexdigest())
            for target in targets
            for path in target.rglob("*") if path.is_file())

    def _linked_worktree(self, plan_name: str) -> tuple[Path, Path, Path]:
        """A detached worktree with one root-level and one nested link."""
        path = self.parent / f"{self.root.name}.worktrees" / plan_name
        path.parent.mkdir(exist_ok=True)
        _git(self.root, "worktree", "add", "--detach", str(path), self.snapshot)
        root_target = self._external(f"{plan_name} pkg")
        nested_target = self._external(f"{plan_name} build")
        make_directory_link(path / "pkg", root_target)
        make_directory_link(path / "lib" / "build", nested_target)
        return path, root_target, nested_target

    def _listed_worktrees(self) -> list[Path]:
        return [Path(line.removeprefix("worktree ")).resolve()
                for line in _git(
                    self.root, "worktree", "list", "--porcelain").splitlines()
                if line.startswith("worktree ")]

    def test_the_inventory_lists_every_link_without_entering_one(self) -> None:
        path, root_target, nested_target = self._linked_worktree("plan01")
        opened: list[Path] = []
        real_scandir = os.scandir

        def recording_scandir(directory):
            opened.append(Path(directory))
            return real_scandir(directory)

        with mock.patch("os.scandir", recording_scandir):
            links = pathops.inventory_directory_links(path)

        self.assertEqual(links, (path / "lib" / "build", path / "pkg"))
        # Only the worktree's ordinary directories were ever opened: neither
        # link, and nothing on the far side of one.
        self.assertEqual(sorted(opened), [path, path / "lib"])
        self.assertTrue((root_target / "sentinel.txt").is_file())
        self.assertTrue((nested_target / "sentinel.txt").is_file())

    def test_ordinary_removal_keeps_every_linked_target_unchanged(self) -> None:
        path, root_target, nested_target = self._linked_worktree("plan01")
        before = self._contents(root_target, nested_target)

        gitops.remove_worktree(self.root, path)

        self.assertFalse(path.exists())
        self.assertEqual(self._listed_worktrees(), [self.root.resolve()])
        self.assertEqual(self._contents(root_target, nested_target), before)

    def test_a_target_inside_the_main_worktree_survives_removal(self) -> None:
        path, _root_target, _nested_target = self._linked_worktree("plan01")
        inside = self.root / "generated"
        inside.mkdir()
        (inside / "sentinel.txt").write_text("main tree\n", encoding="utf-8")
        make_directory_link(path / "generated", inside)

        gitops.remove_worktree(self.root, path)

        self.assertFalse(path.exists())
        self.assertEqual([entry.name for entry in inside.iterdir()],
                         ["sentinel.txt"])
        self.assertEqual((inside / "sentinel.txt").read_text(encoding="utf-8"),
                         "main tree\n")

    def test_forced_temporary_cleanup_keeps_every_linked_target_unchanged(
            self) -> None:
        root_target = self._external("temporary pkg")
        nested_target = self._external("temporary build")
        before = self._contents(root_target, nested_target)

        with temporary_integration_worktree(
                self.root, "plan01", self.snapshot) as (path, branch):
            make_directory_link(path / "pkg", root_target)
            make_directory_link(path / "lib" / "build", nested_target)

        self.assertFalse(path.exists())
        self.assertEqual(self._listed_worktrees(), [self.root.resolve()])
        self.assertNotIn(branch, plan_branches(self.root, "assent-integration"))
        self.assertEqual(self._contents(root_target, nested_target), before)

    def test_an_unclean_worktree_is_refused_with_its_links_retained(self) -> None:
        path, root_target, nested_target = self._linked_worktree("plan01")
        before = self._contents(root_target, nested_target)
        (path / "README.md").write_text("uncommitted\n", encoding="utf-8")

        with self.assertRaises(AssentError):
            gitops.remove_worktree(self.root, path)

        # Git's own refusal still retains the worktree in full, links included.
        self.assertTrue((path / "pkg" / "sentinel.txt").is_file())
        self.assertTrue((path / "lib" / "build" / "sentinel.txt").is_file())
        self.assertEqual(self._contents(root_target, nested_target), before)

    def test_a_refused_boundary_starts_neither_git_nor_the_fallback(self) -> None:
        failures = (
            ("inventory", "inventory_directory_links",
             AssentError("simulated inventory refusal")),
            ("detachment", "detach_directory_link",
             OSError("simulated detachment refusal")),
        )
        for label, helper, failure in failures:
            with self.subTest(failure=label):
                path, root_target, nested_target = self._linked_worktree(
                    f"plan {label}")
                before = self._contents(root_target, nested_target)
                with mock.patch.object(pathops, helper, side_effect=failure), \
                        mock.patch.object(gitops.shutil, "rmtree") as rmtree:
                    with self.assertRaises(AssentError):
                        gitops.remove_worktree(self.root, path)
                    with self.assertRaisesRegex(
                            AssentError, "cleanup was incomplete"):
                        gitops._cleanup_temporary_worktree(self.root, path)

                rmtree.assert_not_called()
                self.assertIn(path.resolve(), self._listed_worktrees())
                self.assertTrue((path / "pkg" / "sentinel.txt").is_file())
                self.assertEqual(
                    self._contents(root_target, nested_target), before)
                cleanup_worktree(self.root, path)


if __name__ == "__main__":
    unittest.main()
