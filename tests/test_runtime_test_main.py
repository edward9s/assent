"""Main runtime-test candidate lifecycle tests."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import engine, gitops
from assent.config import load_main_runtime_config
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result
from tests.test_runtime_test_action import python_command


class MainRuntimeTestTests(EngineTestCase):
    def setUp(self):
        super().setUp()
        self.write_task(1, status="DONE")
        (self.root / "value.txt").write_text("bad\n", encoding="utf-8")
        self.commit_all()
        self.candidate = gitops.runtime_test_worktree_path(self.root)
        self.addCleanup(self._remove_candidate)

    def _remove_candidate(self):
        if self.candidate.exists() and gitops.is_repo_worktree(
                self.root, self.candidate):
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(self.candidate)],
                cwd=self.root, capture_output=True, check=False)
        subprocess.run(
            ["git", "branch", "-D", gitops.RUNTIME_TEST_BRANCH_PREFIX + "main"],
            cwd=self.root, capture_output=True, check=False)

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

    def test_unchanged_pass_uses_exact_main_head_and_cleans_every_artifact(self):
        base = gitops.head_ref(self.root)
        cfg = self.build_main(python_command(
            "from pathlib import Path; raise SystemExit("
            "0 if Path('value.txt').read_text().strip() == 'bad' else 9)"))

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 0)
        self.assertEqual(gitops.head_ref(self.root), base)
        self.assertFalse(self.candidate.exists())
        self.assertFalse(gitops.branch_exists(
            self.root, gitops.RUNTIME_TEST_BRANCH_PREFIX + "main"))
        self.assertFalse((self.root / ".assent" /
                          "_runtime_test_workflow.toml").exists())
        self.assertIn(base, output)

    def test_repair_is_committed_only_in_candidate_and_pending_rerun_refuses(self):
        base = gitops.head_ref(self.root)
        command = python_command(
            "from pathlib import Path; raise SystemExit("
            "0 if Path('value.txt').read_text().strip() == 'good' else 4)")
        cfg = self.build_main(command, repair=True)

        def repair(_prompt):
            (self.candidate / "value.txt").write_text("good\n", encoding="utf-8")
            return ok_result()

        code, output = self.run_main(cfg, ScriptedAdapter([repair]))

        self.assertEqual(code, 0)
        self.assertEqual(gitops.head_ref(self.root), base)
        self.assertEqual((self.root / "value.txt").read_text(), "bad\n")
        self.assertEqual((self.candidate / "value.txt").read_text(), "good\n")
        self.assertIn(f"Exact base: {base}", output)
        self.assertIn(str(self.candidate), output)

        code, output = self.run_main(cfg, ScriptedAdapter([]))
        self.assertEqual(code, 1)
        self.assertIn("pending human integration", output)
        self.assertEqual((self.candidate / "value.txt").read_text(), "good\n")

    def test_project_command_is_required_before_candidate_creation(self):
        cfg = self.build_main(python_command("raise SystemExit(0)"))
        cfg.provenance["runtime_test.command"] = "user"

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 1)
        self.assertIn("must be stated in the project config", output)
        self.assertFalse(self.candidate.exists())

    def test_failed_workflow_preserves_candidate_state_and_evidence(self):
        cfg = self.build_main(
            python_command("raise SystemExit(7)"), repair=False)

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 1)
        self.assertTrue(self.candidate.is_dir())
        state = self.root / ".assent" / "_runtime_test_workflow.toml"
        self.assertTrue(state.is_file())
        self.assertIn('action_status = "FAILED"', state.read_text())
        self.assertIn("is unresolved", output)

    def test_restart_refuses_changed_candidate_identity_without_reset(self):
        cfg = self.build_main(
            python_command("raise SystemExit(3)"), repair=True)
        code, _output = self.run_main(
            cfg, ScriptedAdapter([lambda _prompt: (_ for _ in ()).throw(
                KeyboardInterrupt())]))
        self.assertEqual(code, 130)
        base = gitops.head_ref(self.candidate)
        subprocess.run(
            ["git", "checkout", "--detach"], cwd=self.candidate,
            capture_output=True, check=True)

        code, output = self.run_main(cfg, ScriptedAdapter([]))

        self.assertEqual(code, 1)
        self.assertIn("expected assent/runtime-test/main", output)
        self.assertEqual(gitops.head_ref(self.candidate), base)
        self.assertIsNone(gitops.current_branch(self.candidate))


if __name__ == "__main__":
    unittest.main()
