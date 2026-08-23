"""Configuration tests for arbitrary finite linear workflows."""
import tempfile
import unittest
from pathlib import Path

from assent import AssentError
from assent.config import Config, WorkflowActionStep, load_config
from tests.engine_support import models_block


ROLES = """
[abilities.implement]
prompt = "Implement the requirement with the smallest coherent design."
writes = true
[abilities.review]
prompt = "Inspect the candidate for correctness and simplicity."
writes = false
[abilities.repair]
prompt = "Review and repair the candidate."
writes = true
[roles.implementer]
ability = ["implement"]
model = "lite"
[roles.reviewer]
ability = ["review"]
model = "core"
[roles.repairer]
ability = ["repair"]
model = "core"
"""


class TestConfig(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".assent").mkdir()

    def load(self, text: str):
        path = self.root / ".assent" / "assent.toml"
        path.write_text(text + models_block(text), encoding="utf-8")
        return load_config(path, "plan01")

    def test_workflow_accepts_arbitrary_role_action_order(self):
        cfg = self.load(ROLES + """
[workflow]
task = [{ role = "reviewer" }, { role = "implementer" },
        { action = "focused_test" }, { role = "repairer" }]
plan = [{ role = "reviewer" }, { role = "repairer" },
        { action = "focused_sweep" }]
integration = [{ role = "reviewer" }, { action = "full_verify" },
               { role = "repairer" }]
""")
        self.assertEqual(len(cfg.workflow_task), 4)
        self.assertEqual(len(cfg.workflow_plan), 3)
        self.assertEqual(len(cfg.workflow_integration), 3)

    def test_actions_are_layer_specific(self):
        cases = (
            ('task = [{ action = "full_verify" }]', "focused_test"),
            ('plan = [{ action = "focused_test" }]', "focused_sweep"),
            ('integration = [{ action = "focused_sweep" }]', "full_verify"),
        )
        for workflow, expected in cases:
            with self.subTest(workflow=workflow), self.assertRaisesRegex(
                    AssentError, expected):
                self.load(ROLES + "[workflow]\n" + workflow + "\n")

    def test_task_array_is_required_and_nonempty(self):
        with self.assertRaisesRegex(AssentError, "must not be empty"):
            self.load(ROLES + """
[workflow]
task = []
plan = [{ role = "repairer" }, { action = "focused_sweep" }]
""")

        with self.assertRaisesRegex(AssentError, "is required"):
            self.load(ROLES)

        with self.assertRaisesRegex(AssentError, r"\[workflow\]\.task must not be empty"):
            Config(
                root=self.root,
                assent_dir=self.root / ".assent",
                tasks_dir=self.root / ".assent" / "plan01",
                tasks_name="plan01",
                workflow_task=(),
            )

    def test_action_only_task_workflow_is_explicit(self):
        cfg = self.load(ROLES + """
[workflow]
task = [{ action = "focused_test" }]
""")
        self.assertEqual(len(cfg.workflow_task), 1)
        self.assertIsInstance(cfg.workflow_task[0], WorkflowActionStep)

    def test_removed_retry_setting_is_not_silently_ignored(self):
        with self.assertRaisesRegex(AssentError, "retry_per_task"):
            self.load(ROLES + "[run]\nretry_per_task = 1\n")

    def test_plan_and_integration_roles_require_models(self):
        text = """
[abilities.work]
prompt = "Work."
writes = true
[roles.bare]
ability = ["work"]
[workflow]
task = [{ action = "focused_test" }]
plan = [{ role = "bare" }]
"""
        with self.assertRaisesRegex(AssentError, "must state model"):
            self.load(text)

    def test_shipped_workflow_uses_combined_repair_roles(self):
        template = (Path(__file__).resolve().parents[1]
                    / "assent" / "templates" / "assent.toml")
        cfg = self.load(template.read_text(encoding="utf-8"))
        task_roles = cfg.workflow_task[::2]
        task_actions = cfg.workflow_task[1::2]
        self.assertTrue(task_roles)
        self.assertEqual(task_roles[0].role, "implementer")
        self.assertTrue(all(step.role == "task_repairer"
                            for step in task_roles[1:]))
        self.assertEqual(len(task_roles), len(task_actions))
        self.assertTrue(all(isinstance(step, WorkflowActionStep)
                            and step.action == "focused_test"
                            for step in task_actions))

        integration_actions = cfg.workflow_integration[::2]
        integration_roles = cfg.workflow_integration[1::2]
        self.assertTrue(integration_roles)
        self.assertEqual(len(integration_actions), len(integration_roles) + 1)
        self.assertTrue(all(isinstance(step, WorkflowActionStep)
                            and step.action == "full_verify"
                            for step in integration_actions))
        self.assertTrue(all(step.role == "integration_repairer"
                            for step in integration_roles))


if __name__ == "__main__":
    unittest.main()
