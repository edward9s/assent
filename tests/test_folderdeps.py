"""Tests for folder-level dependency parsing, completion inference, and cycle checks."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from assent import AssentError
from assent.folderdeps import (find_unfinished_prerequisites,
                               infer_folder_completion,
                               parse_folder_dependencies,
                               parse_folder_dependency_graph,
                               resolve_folder_base)
from assent.gitops import worktree_path

_OK = 'python -c "raise SystemExit(0)"'


def task_text(status: str = "TODO") -> str:
    """Produce formal task file content that ``Plan`` can parse."""
    return "\n".join((
        'title = "Task"',
        "deps = []",
        'model = "lite"',
        f"status = {json.dumps(status)}",
        'scope = ["src/"]',
        f"verify = {json.dumps(_OK)}",
        'goal = """',
        "Finish the work.",
        '"""',
        'acceptance = """',
        "- done",
        '"""',
        "",
    ))


class FolderDepsTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_folder(self, name: str, *statuses: str) -> Path:
        folder = self.assent_dir / name
        folder.mkdir()
        for index, status in enumerate(statuses, 1):
            (folder / f"t{index:03d}_task.e.toml").write_text(
                task_text(status), encoding="utf-8")
        return folder

    def write_roster(self, *names: str) -> None:
        """Write an archive roster listing ``names`` (no live directory needed)."""
        lines: list[str] = []
        for name in names:
            lines.extend((
                "[[archived]]",
                f"folder = {json.dumps(name)}",
                'archived_at = "2026-07-25T00:00:00+00:00"',
                "",
            ))
        (self.assent_dir / "_archived.toml").write_text(
            "\n".join(lines), encoding="utf-8")


class TestParseFolderDependencies(FolderDepsTestCase):
    def test_valid_declaration(self):
        first = self.make_folder("first", "DONE")
        second = self.make_folder("second", "TODO")
        (second / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")

        dependencies = parse_folder_dependencies(second)

        self.assertEqual(dependencies.name, "second")
        self.assertEqual(dependencies.after, ["first"])
        self.assertEqual(dependencies.path, (second / "_folder.toml").resolve())

    def test_empty_after(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text("after = []\n", encoding="utf-8")
        self.assertEqual(parse_folder_dependencies(folder).after, [])

    def test_missing_file_means_no_dependencies(self):
        folder = self.make_folder("work", "TODO")
        self.assertEqual(parse_folder_dependencies(folder).after, [])

    def test_unknown_key_lists_valid_key(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = []\nbefore = ["other"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, r"unknown keys.*valid keys: after"):
            parse_folder_dependencies(folder)

    def test_after_must_be_string_array(self):
        folder = self.make_folder("work", "TODO")
        for value in ('"first"', '["first", 2]'):
            (folder / "_folder.toml").write_text(
                f"after = {value}\n", encoding="utf-8")
            with self.assertRaisesRegex(AssentError, "array of strings"):
                parse_folder_dependencies(folder)

    def test_self_dependency_rejected(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["work"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "must not depend on itself"):
            parse_folder_dependencies(folder)

    def test_invalid_dependency_name_rejected(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["bad/name"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not a valid task folder name"):
            parse_folder_dependencies(folder)

    def test_missing_folder_rejected(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "does not exist"):
            parse_folder_dependencies(folder)

    def test_folder_without_tasks_rejected(self):
        empty = self.assent_dir / "empty"
        empty.mkdir()
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["empty"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "no task files"):
            parse_folder_dependencies(folder)

    def test_folder_config_is_not_treated_as_task(self):
        folder = self.make_folder("work", "DONE")
        (folder / "_folder.toml").write_text("after = []\n", encoding="utf-8")
        result = infer_folder_completion(folder)
        self.assertTrue(result.complete)

    def test_archived_dependency_resolves_without_live_folder(self):
        # "first" has been archived (no live directory), yet the after reference
        # still resolves through the roster.
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        self.write_roster("first")
        self.assertEqual(parse_folder_dependencies(folder).after, ["first"])

    def test_missing_folder_message_notes_roster_was_checked(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")
        self.write_roster("other")
        with self.assertRaisesRegex(
                AssentError, "does not exist.*not in the archive roster"):
            parse_folder_dependencies(folder)

    def test_live_and_archived_same_name_fails_closed(self):
        self.make_folder("dup", "DONE")
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["dup"]\n', encoding="utf-8")
        self.write_roster("dup")
        with self.assertRaisesRegex(
                AssentError, "both as a live task folder and in the archive roster"):
            parse_folder_dependencies(folder)

    def test_malformed_roster_fails_closed(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        (self.assent_dir / "_archived.toml").write_text(
            "archived = [\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not valid TOML"):
            parse_folder_dependencies(folder)


class TestInferFolderCompletion(FolderDepsTestCase):
    def test_all_done_and_skip_is_complete(self):
        folder = self.make_folder("work", "DONE", "SKIP")
        result = infer_folder_completion(folder)
        self.assertTrue(result.complete)
        self.assertIn("DONE or SKIP", result.reason)

    def test_todo_is_incomplete(self):
        result = infer_folder_completion(self.make_folder("work", "TODO"))
        self.assertFalse(result.complete)
        self.assertIn("t001=TODO", result.reason)

    def test_blocked_is_incomplete(self):
        result = infer_folder_completion(self.make_folder("work", "BLOCKED"))
        self.assertFalse(result.complete)
        self.assertIn("t001=BLOCKED", result.reason)

    def test_bad_task_file_is_incomplete_with_reason(self):
        folder = self.make_folder("work", "DONE")
        (folder / "t001_task.e.toml").write_text(
            "status = [\n", encoding="utf-8")
        result = infer_folder_completion(folder)
        self.assertFalse(result.complete)
        self.assertIn("TOML", result.reason)

    def test_no_tasks_is_incomplete_with_reason(self):
        folder = self.make_folder("work")
        result = infer_folder_completion(folder)
        self.assertFalse(result.complete)
        self.assertIn("no task files", result.reason)

    def test_unfinished_prerequisites_include_status_counts(self):
        self.make_folder("base", "TODO", "WIP", "BLOCKED", "DONE", "SKIP")
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")

        result = find_unfinished_prerequisites(folder)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].total, 3)
        self.assertEqual(
            result[0].message(),
            "Prerequisite folder base still has 3 unfinished task(s) (TODO 1, WIP 1, BLOCKED 1)")

    def test_archived_prerequisite_counts_as_finished(self):
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["arch"]\n', encoding="utf-8")
        self.write_roster("arch")
        self.assertEqual(find_unfinished_prerequisites(folder), [])

    def test_archived_and_live_unfinished_reports_only_live(self):
        self.make_folder("live", "TODO")
        folder = self.make_folder("work", "TODO")
        (folder / "_folder.toml").write_text(
            'after = ["arch", "live"]\n', encoding="utf-8")
        self.write_roster("arch")

        result = find_unfinished_prerequisites(folder)

        self.assertEqual([item.name for item in result], ["live"])


class TestFolderDependencyGraph(FolderDepsTestCase):
    def test_acyclic_graph_parsed(self):
        self.make_folder("first", "DONE")
        second = self.make_folder("second", "TODO")
        (second / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        graph = parse_folder_dependency_graph(self.assent_dir)
        self.assertEqual(graph["second"].after, ["first"])

    def test_cycle_reports_complete_path(self):
        first = self.make_folder("first", "TODO")
        second = self.make_folder("second", "TODO")
        third = self.make_folder("third", "TODO")
        (first / "_folder.toml").write_text(
            'after = ["second"]\n', encoding="utf-8")
        (second / "_folder.toml").write_text(
            'after = ["third"]\n', encoding="utf-8")
        (third / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        with self.assertRaisesRegex(
                AssentError, "first -> second -> third -> first"):
            parse_folder_dependency_graph(self.assent_dir)

    def test_archived_upstream_is_a_terminal_leaf(self):
        # An archived upstream has no live directory (so it is not a graph node)
        # yet must resolve as a leaf without a KeyError or a spurious cycle.
        second = self.make_folder("second", "TODO")
        (second / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        self.write_roster("first")
        graph = parse_folder_dependency_graph(self.assent_dir)
        self.assertEqual(graph["second"].after, ["first"])
        self.assertNotIn("first", graph)

    def test_missing_roster_leaves_resolution_unchanged(self):
        self.make_folder("first", "DONE")
        second = self.make_folder("second", "TODO")
        (second / "_folder.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        self.assertFalse((self.assent_dir / "_archived.toml").exists())
        graph = parse_folder_dependency_graph(self.assent_dir)
        self.assertEqual(graph["second"].after, ["first"])


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        check=True).stdout.strip()


class ResolveFolderBaseTestCase(FolderDepsTestCase):
    def setUp(self):
        super().setUp()
        _git(self.root, "init")
        _git(self.root, "config", "user.name", "Test")
        _git(self.root, "config", "user.email", "test@example.com")
        (self.root / ".gitignore").write_text(".assent/\n", encoding="utf-8")
        (self.root / "README.md").write_text("target\n", encoding="utf-8")
        _git(self.root, "add", "-A")
        _git(self.root, "commit", "-m", "target")
        self.addCleanup(self._cleanup_worktrees)

    def _cleanup_worktrees(self) -> None:
        container = self.root.parent / f"{self.root.name}.worktrees"
        if not container.exists():
            return
        for path in container.iterdir():
            if (path / ".git").is_file():
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(path)],
                    cwd=self.root, capture_output=True, encoding="utf-8")
            else:
                shutil.rmtree(path, ignore_errors=True)
        if container.exists():
            container.rmdir()

    def make_source(self, folder: str, start: str = "HEAD") -> tuple[Path, str]:
        path = worktree_path(self.root, folder)
        _git(self.root, "worktree", "add", "-b", f"{folder}/run", str(path), start)
        (path / f"{folder}.txt").write_text(f"{folder}\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", f"finish {folder}")
        return path, _git(self.root, "rev-parse", f"{folder}/run")


class TestResolveFolderBase(ResolveFolderBaseTestCase):
    def test_zero_unaccepted_uses_exact_target_head(self):
        downstream = self.make_folder("downstream", "TODO")
        target = _git(self.root, "rev-parse", "HEAD")

        result = resolve_folder_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, target)

    def test_linked_worktree_caller_still_snapshots_main_target(self):
        downstream = self.make_folder("downstream", "TODO")
        linked, linked_tip = self.make_source("downstream")
        target = _git(self.root, "rev-parse", "HEAD")
        self.assertNotEqual(linked_tip, target)

        result = resolve_folder_base(linked, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertEqual(result.resolved_base, target)

    def test_one_unaccepted_uses_exact_upstream_tip(self):
        self.make_folder("upstream", "DONE", "SKIP")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        _, tip = self.make_source("upstream")
        target = _git(self.root, "rev-parse", "HEAD")

        result = resolve_folder_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertEqual(result.speculative_upstream.folder, "upstream")
        self.assertEqual(result.speculative_upstream.tip, tip)
        self.assertEqual(result.resolved_base, tip)

    def test_mixed_accepted_and_unaccepted_uses_only_unaccepted_tip(self):
        self.make_folder("accepted", "DONE")
        self.make_folder("pending", "DONE")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["accepted", "pending"]\n', encoding="utf-8")
        _, accepted_tip = self.make_source("accepted")
        _git(self.root, "merge", "--ff-only", accepted_tip)
        _, pending_tip = self.make_source("pending")

        result = resolve_folder_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, accepted_tip)
        self.assertEqual(result.speculative_upstream.folder, "pending")
        self.assertEqual(result.resolved_base, pending_tip)

    def test_multiple_unaccepted_lists_evidence_without_state_changes(self):
        self.make_folder("first", "DONE")
        self.make_folder("second", "DONE")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["first", "second"]\n', encoding="utf-8")
        _, first_tip = self.make_source("first")
        # Even an apparent topology between two unaccepted branches must not turn
        # the resolver into a multi-branch integration engine.
        _, second_tip = self.make_source("second", first_tip)
        before = (
            _git(self.root, "status", "--porcelain"),
            _git(self.root, "show-ref", "--heads"),
            _git(self.root, "worktree", "list", "--porcelain"),
        )

        with self.assertRaises(AssentError) as raised:
            resolve_folder_base(self.root, downstream)

        message = str(raised.exception)
        for folder, tip in (("first", first_tip), ("second", second_tip)):
            with self.subTest(folder=folder):
                self.assertIn(f"folder {folder}, tip {tip}", message)
                self.assertIn("accept this upstream", message)
        after = (
            _git(self.root, "status", "--porcelain"),
            _git(self.root, "show-ref", "--heads"),
            _git(self.root, "worktree", "list", "--porcelain"),
        )
        self.assertEqual(after, before)

    def test_incomplete_and_blocked_upstreams_fail_closed(self):
        upstream = self.make_folder("upstream", "DONE")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        self.make_source("upstream")

        for status in ("TODO", "BLOCKED"):
            with self.subTest(status=status):
                (upstream / "t001_task.e.toml").write_text(
                    task_text(status), encoding="utf-8")
                with self.assertRaisesRegex(
                        AssentError, f"upstream folder upstream is incomplete:.*{status}"):
                    resolve_folder_base(self.root, downstream)

    def test_dependency_parse_error_and_cycle_fail_before_git_resolution(self):
        upstream = self.make_folder("upstream", "DONE")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        (upstream / "_folder.toml").write_text(
            'after = ["downstream"]\n', encoding="utf-8")

        with self.assertRaisesRegex(AssentError, "dependencies form a cycle"):
            resolve_folder_base(self.root, downstream)

        (upstream / "_folder.toml").write_text("after = [\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not valid TOML"):
            resolve_folder_base(self.root, downstream)

    def test_advanced_upstream_reports_old_and_new_tips_without_rewrite(self):
        self.make_folder("upstream", "DONE")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        upstream_worktree, old_tip = self.make_source("upstream")
        downstream_worktree, _ = self.make_source("downstream", old_tip)
        (downstream_worktree / "downstream.txt").write_text(
            "downstream advanced\n", encoding="utf-8")
        _git(downstream_worktree, "add", "-A")
        _git(downstream_worktree, "commit", "-m", "downstream work")
        downstream_tip = _git(self.root, "rev-parse", "downstream/run")
        (upstream_worktree / "upstream.txt").write_text(
            "upstream advanced\n", encoding="utf-8")
        _git(upstream_worktree, "add", "-A")
        _git(upstream_worktree, "commit", "-m", "advance upstream")
        new_tip = _git(self.root, "rev-parse", "upstream/run")

        with self.assertRaises(AssentError) as raised:
            resolve_folder_base(
                self.root, downstream, downstream_tip="downstream/run")

        message = str(raised.exception)
        self.assertIn(f"old upstream tip {old_tip}", message)
        self.assertIn(f"current upstream upstream tip {new_tip}", message)
        self.assertIn("assent rework downstream", message)
        self.assertIn("replan", message)
        self.assertEqual(_git(self.root, "rev-parse", "downstream/run"), downstream_tip)

    def test_existing_downstream_must_descend_from_resolved_upstream(self):
        self.make_folder("upstream", "DONE")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        _, upstream_tip = self.make_source("upstream")
        _, downstream_tip = self.make_source("downstream", upstream_tip)

        result = resolve_folder_base(
            self.root, downstream, downstream_tip=downstream_tip)

        self.assertEqual(result.resolved_base, upstream_tip)

    def test_archived_upstream_resolves_to_target_without_speculation(self):
        # An archived upstream (roster only, no live folder or branch) is proven
        # integrated, so the base is the exact target HEAD with no speculation.
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        self.write_roster("upstream")
        target = _git(self.root, "rev-parse", "HEAD")

        result = resolve_folder_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, target)

    def test_archived_upstream_ignored_beside_one_live_speculative(self):
        self.make_folder("live", "DONE", "SKIP")
        downstream = self.make_folder("downstream", "TODO")
        (downstream / "_folder.toml").write_text(
            'after = ["arch", "live"]\n', encoding="utf-8")
        self.write_roster("arch")
        _, tip = self.make_source("live")

        result = resolve_folder_base(self.root, downstream)

        self.assertEqual(result.speculative_upstream.folder, "live")
        self.assertEqual(result.resolved_base, tip)

    def test_sha1_and_sha256_object_ids_are_preserved_exactly(self):
        for object_format, oid_length in (("sha1", 40), ("sha256", 64)):
            with self.subTest(object_format=object_format):
                root = Path(tempfile.mkdtemp())
                assent_dir = root / ".assent"
                container = root.parent / f"{root.name}.worktrees"
                try:
                    assent_dir.mkdir()
                    _git(root, "init", f"--object-format={object_format}")
                    _git(root, "config", "user.name", "Test")
                    _git(root, "config", "user.email", "test@example.com")
                    (root / ".gitignore").write_text(
                        ".assent/\n", encoding="utf-8")
                    (root / "README.md").write_text(
                        "target\n", encoding="utf-8")
                    _git(root, "add", "-A")
                    _git(root, "commit", "-m", "target")
                    upstream = assent_dir / "upstream"
                    downstream = assent_dir / "downstream"
                    upstream.mkdir()
                    downstream.mkdir()
                    (upstream / "t001_task.e.toml").write_text(
                        task_text("DONE"), encoding="utf-8")
                    (downstream / "t001_task.e.toml").write_text(
                        task_text("TODO"), encoding="utf-8")
                    (downstream / "_folder.toml").write_text(
                        'after = ["upstream"]\n', encoding="utf-8")
                    source = worktree_path(root, "upstream")
                    _git(root, "worktree", "add", "-b", "upstream/run",
                         str(source), "HEAD")
                    (source / "upstream.txt").write_text(
                        "source\n", encoding="utf-8")
                    _git(source, "add", "-A")
                    _git(source, "commit", "-m", "finish upstream")

                    result = resolve_folder_base(root, downstream)

                    self.assertEqual(len(result.target_snapshot), oid_length)
                    self.assertEqual(len(result.resolved_base), oid_length)
                    self.assertEqual(
                        result.resolved_base,
                        _git(root, "rev-parse", "upstream/run"))
                finally:
                    if container.exists():
                        for path in container.iterdir():
                            if (path / ".git").is_file():
                                subprocess.run(
                                    ["git", "worktree", "remove", "--force", str(path)],
                                    cwd=root, capture_output=True, encoding="utf-8")
                            else:
                                shutil.rmtree(path, ignore_errors=True)
                        if container.exists():
                            container.rmdir()
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
