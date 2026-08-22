"""Task contracts and deletable workflow cursors."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from assent import AssentError
from assent.plan import (
    Plan, SelectionWorkflowState, WorkflowState, parse_task_file,
    plan_workflow_requires_human,
    read_selection_workflow_state, read_workflow_state,
    set_status, write_selection_workflow_state, write_workflow_state,
)


def task_text(*, status="TODO", extra="") -> str:
    return "\n".join((
        'title = "Task"',
        'deps = []',
        'model = "lite"',
        f"status = {json.dumps(status)}",
        'verify = "python -c \\"raise SystemExit(0)\\""',
        'goal = "Do one thing."',
        'behavior = "Keep the design small."',
        'acceptance = "- done"',
        extra,
        "",
    ))


class TestPlan(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write(self, name="t001_task.e.toml", text=None) -> Path:
        path = self.directory / name
        path.write_text(text or task_text(), encoding="utf-8")
        return path

    def test_task_schema_has_no_path_scope(self):
        task = parse_task_file(self.write())
        self.assertEqual(task.id, "t001")
        self.assertFalse(hasattr(task, "scope"))
        with self.assertRaisesRegex(AssentError, "undefined fields: scope"):
            parse_task_file(self.write(
                "t002_bad.e.toml", task_text(extra='scope = ["src/"]')))

    def test_task_workflow_uses_the_project_role_action_shape(self):
        task = parse_task_file(self.write(text=task_text(extra=(
            'workflow = [{ role = "tests_writer" }, '
            '{ action = "focused_test" }]'))))
        self.assertEqual(task.workflow[0], "tests_writer")
        self.assertEqual(task.workflow[1].action, "focused_test")

        with self.assertRaisesRegex(AssentError, "must be an inline table"):
            parse_task_file(self.write(
                "t002_bad.e.toml",
                task_text(extra='workflow = ["tests_writer", "@focused_test"]')))

    def test_task_workflow_refuses_empty_and_accepts_explicit_action_only(self):
        with self.assertRaisesRegex(AssentError, "must not be empty"):
            parse_task_file(self.write(
                "t002_empty.e.toml", task_text(extra="workflow = []")))

        task = parse_task_file(self.write(
            "t003_verify.e.toml",
            task_text(extra='workflow = [{ action = "focused_test" }]')))
        self.assertEqual(task.workflow[0].action, "focused_test")

    def test_scheduler_status_write_changes_only_status(self):
        path = self.write()
        before = parse_task_file(path)
        set_status(path, "WIP")
        after = parse_task_file(path)
        self.assertEqual(after.status, "WIP")
        self.assertEqual(after, replace(before, status="WIP"))

    def test_plan_dependency_order_is_validated(self):
        self.write("t001_first.e.toml")
        second = task_text().replace('deps = []', 'deps = ["t001"]')
        self.write("t002_second.e.toml", second)
        plan = Plan.parse(self.directory)
        self.assertEqual([task.id for task in plan.tasks], ["t001", "t002"])

    def test_workflow_state_round_trip(self):
        state = WorkflowState(
            "task", "t001", 2, False, "base", ("role evidence",),
            "focused_test", "FAILED", "tree", 1, ("command", "summary"))
        write_workflow_state(self.directory, state)
        self.assertEqual(read_workflow_state(self.directory), state)

    def test_failed_plan_workflow_requires_human(self):
        state = WorkflowState(
            "plan", "", 1, False, "base", action="focused_sweep",
            action_status="FAILED", action_source_tree="tree", action_exit_code=1,
            action_evidence=("command", "summary"))
        write_workflow_state(self.directory, state)
        self.assertTrue(plan_workflow_requires_human(self.directory))

    def test_selection_state_round_trip(self):
        state = SelectionWorkflowState(
            ("plan01",), "main", "target", ("source",), 1,
            ("repair evidence",), "full_verify", "FAILED", "candidate", 1,
            ("VERIFIER_FAILED",), "verify-digest", "shared-digest")
        write_selection_workflow_state(self.directory, state)
        self.assertEqual(read_selection_workflow_state(self.directory), state)


if __name__ == "__main__":
    unittest.main()
