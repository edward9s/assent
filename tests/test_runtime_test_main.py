"""Main runtime-test working-tree lifecycle tests."""
import contextlib
import io
import json
import unittest
from unittest import mock

from assent import engine, gitops
from assent.config import load_main_runtime_config
from assent.plan import WorkflowState, write_runtime_test_workflow_state
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result
from tests.test_runtime_test_action import python_command


class MainRuntimeTestTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.write_task(1, status="DONE")
        (self.root / "value.txt").write_text("bad\n", encoding="utf-8")
        self.commit_all()
        self.obsolete_candidate = (
            self.root.parent / f"{self.root.name}.runtime-test" / "main")

    def build_main(self, command: str, *, repair: bool = False):
        workflow = ['{ action = "runtime_test" }']
        role = ""
        if repair:
            workflow += ['{ role = "writer", model = "lite" }',
                         '{ action = "runtime_test" }']
            role = ("[abilities.write]\n"
                    'prompt = "Repair the runtime failure."\n'
                    "writes = true\n"
                    "[roles.writer]\n"
                    'ability = ["write"]\n')
        text = (
            '[adapter]\nname = "claude"\n'
            '[adapter.claude]\ncommand = "python"\n'
            '[adapter.claude.models]\n'
            'prime = "fable/high"\ncore = "opus/high"\n'
            'lite = "sonnet/medium"\n'
            + role
            + "[runtime_test]\ncommand = "
            + json.dumps(command)
            + "\n[workflow]\n"
            + 'task = [{ action = "focused_test" }]\n'
            + "runtime_test = [" + ", ".join(workflow) + "]\n")
        path = self.root / ".assent" / "assent.toml"
        path.write_text(text, encoding="utf-8")
        return load_main_runtime_config(path)

    def run_main(self, cfg, adapter):
        output = io.StringIO()
        with mock.patch.object(engine.contracts, "require_contracts"), \
                contextlib.redirect_stdout(output):
            code = engine.run_main_runtime_test(
                cfg, adapter=adapter, sleep=lambda _seconds: None)
        return code, output.getvalue()

    def test_unchanged_pass_runs_in_primary_and_leaves_no_runtime_state(self):
        base = gitops.head_ref(self.root)
        cfg = self.build_main(python_command(
            "from pathlib import Path; raise SystemExit("
            "0 if Path('value.txt').read_text().strip() == 'bad' else 9)"))

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 0)
        self.assertEqual(gitops.head_ref(self.root), base)
        self.assertFalse(self.obsolete_candidate.exists())
        self.assertFalse((self.root / ".assent" /
                          "_runtime_test_workflow.toml").exists())
        self.assertIn(str(self.root), output)

    def test_repair_edits_primary_without_creating_a_commit_or_candidate(self):
        base = gitops.head_ref(self.root)
        command = python_command(
            "from pathlib import Path; raise SystemExit("
            "0 if Path('value.txt').read_text().strip() == 'good' else 4)")
        cfg = self.build_main(command, repair=True)

        def repair(prompt):
            self.assertNotIn("ignored-dirs", prompt)
            (self.root / "value.txt").write_text("good\n", encoding="utf-8")
            return ok_result()

        with mock.patch.object(
                engine, "_ignored_dir_decision",
                side_effect=AssertionError(
                    "main runtime must not classify ignored directories")):
            code, output = self.run_main(cfg, ScriptedAdapter([repair]))

        self.assertEqual(code, 0)
        self.assertEqual(gitops.head_ref(self.root), base)
        self.assertEqual((self.root / "value.txt").read_text(), "good\n")
        self.assertFalse(self.obsolete_candidate.exists())
        self.assertIn("passed in current working tree", output)

    def test_repair_detects_content_change_in_an_already_dirty_file(self):
        (self.root / "value.txt").write_text("preexisting\n", encoding="utf-8")
        base = gitops.head_ref(self.root)
        command = python_command(
            "from pathlib import Path; raise SystemExit("
            "0 if Path('value.txt').read_text().strip() == 'good' else 4)")
        cfg = self.build_main(command, repair=True)

        def repair(_prompt):
            (self.root / "value.txt").write_text("good\n", encoding="utf-8")
            return ok_result()

        code, _output = self.run_main(cfg, ScriptedAdapter([repair]))

        self.assertEqual(code, 0)
        self.assertEqual(gitops.head_ref(self.root), base)
        self.assertEqual((self.root / "value.txt").read_text(), "good\n")

    def test_project_command_is_required_before_execution(self):
        cfg = self.build_main(python_command("raise SystemExit(0)"))
        cfg.provenance["runtime_test.command"] = "user"

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 1)
        self.assertIn("must be stated in the project config", output)
        self.assertFalse(self.obsolete_candidate.exists())

    def test_separate_legacy_source_is_preserved_and_refused(self):
        cfg = self.build_main(python_command("raise SystemExit(0)"))
        base = gitops.head_ref(self.root)
        write_runtime_test_workflow_state(
            self.root / ".assent",
            WorkflowState(
                "runtime_test", "", 1, False, base,
                action="runtime_test", candidate_head="f" * 40))

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 1)
        self.assertIn("runtime state names a separate source HEAD", output)
        self.assertTrue((self.root / ".assent" /
                         "_runtime_test_workflow.toml").is_file())

    def test_failed_workflow_preserves_state_without_a_candidate(self):
        cfg = self.build_main(
            python_command("raise SystemExit(7)"), repair=False)

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 1)
        self.assertFalse(self.obsolete_candidate.exists())
        state = self.root / ".assent" / "_runtime_test_workflow.toml"
        self.assertTrue(state.is_file())
        self.assertIn('action_status = "FAILED"', state.read_text())
        self.assertIn("REVIEW UNRESOLVED", output)

    def test_interrupt_preserves_primary_edits_without_committing(self):
        cfg = self.build_main(
            python_command("raise SystemExit(3)"), repair=True)
        base = gitops.head_ref(self.root)

        def interrupt(_prompt):
            (self.root / "value.txt").write_text("kept\n", encoding="utf-8")
            raise KeyboardInterrupt()

        code, output = self.run_main(cfg, ScriptedAdapter([interrupt]))

        self.assertEqual(code, 130)
        self.assertEqual(gitops.head_ref(self.root), base)
        self.assertEqual((self.root / "value.txt").read_text(), "kept\n")
        self.assertTrue((self.root / ".assent" /
                         "_runtime_test_workflow.toml").is_file())
        self.assertIn("working-tree edits were preserved", output)


if __name__ == "__main__":
    unittest.main()
