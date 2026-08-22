"""Reports present mechanical workflow evidence without an auto-fix ledger."""
import unittest

from assent.inspection import render_report
from assent.plan import (Plan, WorkflowState, append_entry, set_status,
                         write_workflow_state)
from tests.engine_support import EngineTestCase


class TestInspection(EngineTestCase):
    def test_report_contains_task_and_receipt_sections_only(self):
        path = self.write_task(1, title="A task")
        cfg = self.build()
        self.commit_all()
        set_status(path, "BLOCKED")
        task = Plan.parse(cfg.tasks_dir).tasks[0]
        append_entry(
            task.journal_path, by="scheduler", event="blocked",
            summary="Configured workflow exhausted")

        text = render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("t001  BLOCKED", text)
        self.assertIn("Configured workflow exhausted", text)
        self.assertNotIn("Plan auto-fix", text)
        self.assertNotIn("finding owner", text)

    def test_report_names_unresolved_plan_workflow(self):
        self.write_task(1, status="DONE")
        cfg = self.build()
        self.commit_all()
        write_workflow_state(cfg.tasks_dir, WorkflowState(
            "plan", "", 1, False, "HEAD", action="focused_sweep",
            action_status="FAILED", action_source_tree="tree:commands",
            action_exit_code=1, action_evidence=("check", "failed")))

        text = render_report(cfg, Plan.parse(cfg.tasks_dir))
        self.assertIn("REVIEW UNRESOLVED, HUMAN DECISION", text)
        self.assertIn("Last focused_sweep: FAILED", text)


if __name__ == "__main__":
    unittest.main()
