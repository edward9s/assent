"""Tests for folder-level dependency parsing, completion inference, and cycle checks."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from assent import AssentError
from assent.folderdeps import (find_unfinished_prerequisites,
                               infer_folder_completion,
                               parse_folder_dependencies,
                               parse_folder_dependency_graph)

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


if __name__ == "__main__":
    unittest.main()
