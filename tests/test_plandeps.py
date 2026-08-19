"""Tests for plan-level dependency parsing, completion inference, and cycle checks."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from assent import AssentError
from assent.plandeps import (find_unfinished_prerequisites,
                               infer_plan_completion,
                               parse_plan_dependencies,
                               parse_plan_dependency_graph,
                               resolve_plan_base)
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


class PlanDepsTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_plan(self, name: str, *statuses: str) -> Path:
        plan_name = self.assent_dir / name
        plan_name.mkdir()
        for index, status in enumerate(statuses, 1):
            (plan_name / f"t{index:03d}_task.e.toml").write_text(
                task_text(status), encoding="utf-8")
        return plan_name

    def write_roster(self, *names: str) -> None:
        """Write an archive roster listing ``names`` (no live directory needed)."""
        lines: list[str] = []
        for name in names:
            lines.extend((
                "[[archived]]",
                f"plan = {json.dumps(name)}",
                'archived_at = "2026-07-25T00:00:00+00:00"',
                "",
            ))
        (self.assent_dir / "_archived.toml").write_text(
            "\n".join(lines), encoding="utf-8")


class TestParsePlanDependencies(PlanDepsTestCase):
    def test_valid_declaration(self):
        first = self.make_plan("first", "DONE")
        second = self.make_plan("second", "TODO")
        (second / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")

        dependencies = parse_plan_dependencies(second)

        self.assertEqual(dependencies.name, "second")
        self.assertEqual(dependencies.after, ["first"])
        self.assertEqual(dependencies.path, (second / "_plan_deps.toml").resolve())

    def test_empty_after(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text("after = []\n", encoding="utf-8")
        self.assertEqual(parse_plan_dependencies(plan_name).after, [])

    def test_missing_file_means_no_dependencies(self):
        plan_name = self.make_plan("work", "TODO")
        dependencies = parse_plan_dependencies(plan_name)
        self.assertEqual(dependencies.after, [])
        self.assertIsNone(dependencies.base)

    def test_existing_file_requires_after(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'base = "first"\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "missing after"):
            parse_plan_dependencies(plan_name)

    def test_unknown_key_lists_valid_key(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = []\nbefore = ["other"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, r"unknown keys.*valid keys: after"):
            parse_plan_dependencies(plan_name)

    def test_after_must_be_string_array(self):
        plan_name = self.make_plan("work", "TODO")
        for value in ('"first"', '["first", 2]'):
            (plan_name / "_plan_deps.toml").write_text(
                f"after = {value}\n", encoding="utf-8")
            with self.assertRaisesRegex(AssentError, "array of strings"):
                parse_plan_dependencies(plan_name)

    def test_self_dependency_rejected(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["work"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "must not depend on itself"):
            parse_plan_dependencies(plan_name)

    def test_invalid_dependency_name_rejected(self):
        plan_name = self.make_plan("work", "TODO")
        invalid = (
            "", "bad/name", "bad\\name", "bad name", "-bad", ".bad",
            "bad\x00name", "bad~name", "bad^name", "bad:name",
            "bad?name", "bad*name", "bad[name", "bad<name", "bad>name",
            'bad"name', "bad|name", "bad..name", "bad@{name", "bad.",
            "bad.lock", "bad.LOCK", "CON.txt", "COM¹")
        for name in invalid:
            with self.subTest(name=name):
                (plan_name / "_plan_deps.toml").write_text(
                    f"after = {json.dumps([name])}\n", encoding="utf-8")
                with self.assertRaises(AssentError) as raised:
                    parse_plan_dependencies(plan_name)
                self.assertIn(repr(name), str(raised.exception))
                self.assertIn("not a valid plan name", str(raised.exception))

    def test_missing_plan_rejected(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "does not exist"):
            parse_plan_dependencies(plan_name)

    def test_plan_without_tasks_rejected(self):
        empty = self.assent_dir / "empty"
        empty.mkdir()
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["empty"]\n', encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "no task files"):
            parse_plan_dependencies(plan_name)

    def test_plan_config_is_not_treated_as_task(self):
        plan_name = self.make_plan("work", "DONE")
        (plan_name / "_plan_deps.toml").write_text("after = []\n", encoding="utf-8")
        result = infer_plan_completion(plan_name)
        self.assertTrue(result.complete)

    def test_archived_dependency_resolves_without_live_plan(self):
        # "first" has been archived (no live directory), yet the after reference
        # still resolves through the roster.
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        self.write_roster("first")
        self.assertEqual(parse_plan_dependencies(plan_name).after, ["first"])

    def test_missing_plan_message_notes_roster_was_checked(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["missing"]\n', encoding="utf-8")
        self.write_roster("other")
        with self.assertRaisesRegex(
                AssentError, "does not exist.*not in the archive roster"):
            parse_plan_dependencies(plan_name)

    def test_live_and_archived_same_name_fails_closed(self):
        self.make_plan("dup", "DONE")
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["dup"]\n', encoding="utf-8")
        self.write_roster("dup")
        with self.assertRaisesRegex(
                AssentError, "both as a live plan and in the archive roster"):
            parse_plan_dependencies(plan_name)

    def test_malformed_roster_fails_closed(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        (self.assent_dir / "_archived.toml").write_text(
            "archived = [\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not valid TOML"):
            parse_plan_dependencies(plan_name)


class TestInferPlanCompletion(PlanDepsTestCase):
    def test_all_done_and_skip_is_complete(self):
        plan_name = self.make_plan("work", "DONE", "SKIP")
        result = infer_plan_completion(plan_name)
        self.assertTrue(result.complete)
        self.assertIn("DONE or SKIP", result.reason)

    def test_todo_is_incomplete(self):
        result = infer_plan_completion(self.make_plan("work", "TODO"))
        self.assertFalse(result.complete)
        self.assertIn("t001=TODO", result.reason)

    def test_blocked_is_incomplete(self):
        result = infer_plan_completion(self.make_plan("work", "BLOCKED"))
        self.assertFalse(result.complete)
        self.assertIn("t001=BLOCKED", result.reason)

    def test_bad_task_file_is_incomplete_with_reason(self):
        plan_name = self.make_plan("work", "DONE")
        (plan_name / "t001_task.e.toml").write_text(
            "status = [\n", encoding="utf-8")
        result = infer_plan_completion(plan_name)
        self.assertFalse(result.complete)
        self.assertIn("TOML", result.reason)

    def test_no_tasks_is_incomplete_with_reason(self):
        plan_name = self.make_plan("work")
        result = infer_plan_completion(plan_name)
        self.assertFalse(result.complete)
        self.assertIn("no task files", result.reason)

    def test_unfinished_prerequisites_include_status_counts(self):
        self.make_plan("base", "TODO", "WIP", "BLOCKED", "DONE", "SKIP")
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["base"]\n', encoding="utf-8")

        result = find_unfinished_prerequisites(plan_name)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].total, 3)
        self.assertEqual(
            result[0].message(),
            "Prerequisite plan base still has 3 unfinished task(s) (TODO 1, WIP 1, BLOCKED 1)")

    def test_archived_prerequisite_counts_as_finished(self):
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["arch"]\n', encoding="utf-8")
        self.write_roster("arch")
        self.assertEqual(find_unfinished_prerequisites(plan_name), [])

    def test_archived_and_live_unfinished_reports_only_live(self):
        self.make_plan("live", "TODO")
        plan_name = self.make_plan("work", "TODO")
        (plan_name / "_plan_deps.toml").write_text(
            'after = ["arch", "live"]\n', encoding="utf-8")
        self.write_roster("arch")

        result = find_unfinished_prerequisites(plan_name)

        self.assertEqual([item.name for item in result], ["live"])


class TestPlanDependencyGraph(PlanDepsTestCase):
    def test_acyclic_graph_parsed(self):
        self.make_plan("first", "DONE")
        second = self.make_plan("second", "TODO")
        (second / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        graph = parse_plan_dependency_graph(self.assent_dir)
        self.assertEqual(graph["second"].after, ["first"])

    def test_cycle_reports_complete_path(self):
        first = self.make_plan("first", "TODO")
        second = self.make_plan("second", "TODO")
        third = self.make_plan("third", "TODO")
        (first / "_plan_deps.toml").write_text(
            'after = ["second"]\n', encoding="utf-8")
        (second / "_plan_deps.toml").write_text(
            'after = ["third"]\n', encoding="utf-8")
        (third / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        with self.assertRaisesRegex(
                AssentError, "first -> second -> third -> first"):
            parse_plan_dependency_graph(self.assent_dir)

    def test_archived_upstream_is_a_terminal_leaf(self):
        # An archived upstream has no live directory (so it is not a graph node)
        # yet must resolve as a leaf without a KeyError or a spurious cycle.
        second = self.make_plan("second", "TODO")
        (second / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        self.write_roster("first")
        graph = parse_plan_dependency_graph(self.assent_dir)
        self.assertEqual(graph["second"].after, ["first"])
        self.assertNotIn("first", graph)

    def test_missing_roster_leaves_resolution_unchanged(self):
        self.make_plan("first", "DONE")
        second = self.make_plan("second", "TODO")
        (second / "_plan_deps.toml").write_text(
            'after = ["first"]\n', encoding="utf-8")
        self.assertFalse((self.assent_dir / "_archived.toml").exists())
        graph = parse_plan_dependency_graph(self.assent_dir)
        self.assertEqual(graph["second"].after, ["first"])


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, encoding="utf-8",
        check=True).stdout.strip()


class ResolvePlanBaseTestCase(PlanDepsTestCase):
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

    def make_source(self, plan_name: str, start: str = "HEAD") -> tuple[Path, str]:
        path = worktree_path(self.root, plan_name)
        _git(self.root, "worktree", "add", "-b", f"{plan_name}/run", str(path), start)
        (path / f"{plan_name}.txt").write_text(f"{plan_name}\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-m", f"finish {plan_name}")
        return path, _git(self.root, "rev-parse", f"{plan_name}/run")


class TestResolvePlanBase(ResolvePlanBaseTestCase):
    def test_zero_unaccepted_uses_exact_target_head(self):
        downstream = self.make_plan("downstream", "TODO")
        target = _git(self.root, "rev-parse", "HEAD")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, target)

    def test_linked_worktree_caller_still_snapshots_main_target(self):
        downstream = self.make_plan("downstream", "TODO")
        linked, linked_tip = self.make_source("downstream")
        target = _git(self.root, "rev-parse", "HEAD")
        self.assertNotEqual(linked_tip, target)

        result = resolve_plan_base(linked, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertEqual(result.resolved_base, target)

    def test_one_unaccepted_without_base_uses_target_head(self):
        self.make_plan("upstream", "DONE", "SKIP")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        self.make_source("upstream")
        target = _git(self.root, "rev-parse", "HEAD")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, target)

    def test_mixed_accepted_and_unaccepted_without_base_uses_target_head(self):
        self.make_plan("accepted", "DONE")
        self.make_plan("pending", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["accepted", "pending"]\n', encoding="utf-8")
        _, accepted_tip = self.make_source("accepted")
        _git(self.root, "merge", "--ff-only", accepted_tip)
        self.make_source("pending")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, accepted_tip)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, accepted_tip)

    def test_multiple_unaccepted_without_base_use_target_without_state_changes(self):
        self.make_plan("first", "DONE")
        self.make_plan("second", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
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

        result = resolve_plan_base(self.root, downstream)

        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, result.target_snapshot)
        self.assertNotEqual(result.resolved_base, first_tip)
        self.assertNotEqual(result.resolved_base, second_tip)
        after = (
            _git(self.root, "status", "--porcelain"),
            _git(self.root, "show-ref", "--heads"),
            _git(self.root, "worktree", "list", "--porcelain"),
        )
        self.assertEqual(after, before)

    def test_declared_base_selects_one_of_multiple_unaccepted_tips(self):
        self.make_plan("first", "DONE")
        self.make_plan("second", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["first", "second"]\nbase = "first"\n',
            encoding="utf-8")
        _, first_tip = self.make_source("first")
        self.make_source("second")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.speculative_upstream.plan, "first")
        self.assertEqual(result.speculative_upstream.tip, first_tip)
        self.assertEqual(result.resolved_base, first_tip)

    def test_accepted_declared_base_uses_target_despite_unaccepted_peer(self):
        self.make_plan("first", "DONE")
        self.make_plan("second", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["first", "second"]\nbase = "first"\n',
            encoding="utf-8")
        _, first_tip = self.make_source("first")
        _git(self.root, "merge", "--ff-only", first_tip)
        self.make_source("second")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, first_tip)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, first_tip)

    def test_incomplete_and_blocked_upstreams_fail_closed(self):
        upstream = self.make_plan("upstream", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        self.make_source("upstream")

        for status in ("TODO", "BLOCKED"):
            with self.subTest(status=status):
                (upstream / "t001_task.e.toml").write_text(
                    task_text(status), encoding="utf-8")
                with self.assertRaisesRegex(
                        AssentError, f"upstream plan upstream is incomplete:.*{status}"):
                    resolve_plan_base(self.root, downstream)

    def test_dependency_parse_error_and_cycle_fail_before_git_resolution(self):
        upstream = self.make_plan("upstream", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["upstream"]\n', encoding="utf-8")
        (upstream / "_plan_deps.toml").write_text(
            'after = ["downstream"]\n', encoding="utf-8")

        with self.assertRaisesRegex(AssentError, "dependencies form a cycle"):
            resolve_plan_base(self.root, downstream)

        (upstream / "_plan_deps.toml").write_text("after = [\n", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "not valid TOML"):
            resolve_plan_base(self.root, downstream)

    def test_advanced_upstream_reports_old_and_new_tips_without_rewrite(self):
        self.make_plan("upstream", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["upstream"]\nbase = "upstream"\n', encoding="utf-8")
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
            resolve_plan_base(
                self.root, downstream, downstream_tip="downstream/run")

        message = str(raised.exception)
        self.assertIn(f"old upstream tip {old_tip}", message)
        self.assertIn(f"current upstream upstream tip {new_tip}", message)
        self.assertIn("assent rework downstream", message)
        self.assertIn("replan", message)
        self.assertEqual(_git(self.root, "rev-parse", "downstream/run"), downstream_tip)

    def test_existing_downstream_must_descend_from_resolved_upstream(self):
        self.make_plan("upstream", "DONE")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["upstream"]\nbase = "upstream"\n', encoding="utf-8")
        _, upstream_tip = self.make_source("upstream")
        _, downstream_tip = self.make_source("downstream", upstream_tip)

        result = resolve_plan_base(
            self.root, downstream, downstream_tip=downstream_tip)

        self.assertEqual(result.resolved_base, upstream_tip)

    def test_archived_upstream_resolves_to_target_without_speculation(self):
        # An archived upstream (roster only, no live plan or branch) is proven
        # integrated, so the base is the exact target HEAD with no speculation.
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["upstream"]\nbase = "upstream"\n', encoding="utf-8")
        self.write_roster("upstream")
        target = _git(self.root, "rev-parse", "HEAD")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.target_snapshot, target)
        self.assertIsNone(result.speculative_upstream)
        self.assertEqual(result.resolved_base, target)

    def test_archived_upstream_ignored_beside_one_live_speculative(self):
        self.make_plan("live", "DONE", "SKIP")
        downstream = self.make_plan("downstream", "TODO")
        (downstream / "_plan_deps.toml").write_text(
            'after = ["arch", "live"]\nbase = "live"\n', encoding="utf-8")
        self.write_roster("arch")
        _, tip = self.make_source("live")

        result = resolve_plan_base(self.root, downstream)

        self.assertEqual(result.speculative_upstream.plan, "live")
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
                    (downstream / "_plan_deps.toml").write_text(
                        'after = ["upstream"]\nbase = "upstream"\n',
                        encoding="utf-8")
                    source = worktree_path(root, "upstream")
                    _git(root, "worktree", "add", "-b", "upstream/run",
                         str(source), "HEAD")
                    (source / "upstream.txt").write_text(
                        "source\n", encoding="utf-8")
                    _git(source, "add", "-A")
                    _git(source, "commit", "-m", "finish upstream")

                    result = resolve_plan_base(root, downstream)

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
