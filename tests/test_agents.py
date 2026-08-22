"""Tests for ability definitions and freely composed agent roles."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.config import load_config
from assent.user_home import ASSENT_HOME_ENV
from tests.engine_support import models_block


class RoleConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.user_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.user_home, ignore_errors=True)
        self.user_assent = self.user_home / ".assent"
        self.user_assent.mkdir()
        env = mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(self.user_assent)})
        env.start()
        self.addCleanup(env.stop)

    def load(self, text: str):
        # Assent ships no built-in model ids, so every selected adapter needs its
        # tiers stated; these are the fixture's own values.
        path = self.assent_dir / "assent.toml"
        path.write_text(text + models_block(text), encoding="utf-8")
        return load_config(path, "plan01")


class TestRoles(RoleConfigTestCase):
    def test_composed_role_resolves_in_order_and_derives_flags(self):
        cfg = self.load("""
[abilities.build]
prompt = "Implement the requested change."
writes = true

[abilities.review]
prompt = "Return a scheduler verdict."
writes = false
produces_verdict = true

[roles.builder_reviewer]
ability = ["build", "review"]
model = "core"
""")

        resolved = cfg.resolve_role("builder_reviewer")
        self.assertEqual(resolved.abilities,
                         (cfg.abilities["build"], cfg.abilities["review"]))
        self.assertEqual(resolved.model, "core")
        self.assertTrue(resolved.writes)
        self.assertTrue(resolved.produces_verdict)

    def test_review_name_does_not_imply_a_verdict(self):
        cfg = self.load("""
[abilities.review]
prompt = "Inspect without a scheduler verdict."
writes = false
produces_verdict = false

[roles.reader]
ability = ["review"]
""")

        resolved = cfg.resolve_role("reader")
        self.assertFalse(resolved.writes)
        self.assertFalse(resolved.produces_verdict)

    def test_missing_ability_and_empty_role_refuse_to_load(self):
        with self.assertRaisesRegex(AssentError, "missing ability 'absent'"):
            self.load("[roles.worker]\nability = [\"absent\"]\n")
        with self.assertRaisesRegex(AssentError, "non-empty array"):
            self.load("[roles.worker]\nability = []\n")

    def test_removed_agents_table_refuses_as_an_unknown_top_level_key(self):
        with self.assertRaisesRegex(AssentError, "unknown top-level keys: agents"):
            self.load("[agents.worker]\nability = [\"build\"]\n")

    def test_wrong_types_unknown_keys_and_invalid_tiers_refuse_to_load(self):
        cases = (
            ("[abilities.build]\nprompt = 1\nwrites = true\n",
             "wrong type"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = true\nextra = 1\n",
             "unknown keys"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = true\ngate = true\n",
             "unknown keys"),
            ("[roles.worker]\nability = [1]\n", "all-string"),
            ("[roles.worker]\nability = [\"build\"]\nextra = 1\n",
             "unknown keys"),
            # A role may name a vendor selection, so only a malformed one is
            # refused here; the closed tier vocabulary lives in task files.
            ("[abilities.build]\nprompt = \"x\"\nwrites = false\n"
             "[roles.worker]\nability = [\"build\"]\nmodel = \"a/b/c\"\n",
             "must not contain"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = false\n"
             "[roles.worker]\nability = [\"build\"]\neffort = \"high\"\n",
             "unknown keys: effort"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(AssentError, message):
                self.load(text)

    def test_shipped_config_activates_default_roles_and_workflows(self):
        template = (Path(__file__).resolve().parents[1]
                    / "assent" / "templates" / "assent.toml")
        cfg = self.load(template.read_text(encoding="utf-8"))
        self.assertEqual(
            [step.action if hasattr(step, "action") else "role"
             for step in cfg.workflow_task],
            ["role", "focused_test", "role", "role", "focused_test"])
        self.assertEqual(
            [step.action if hasattr(step, "action") else "role"
             for step in cfg.workflow_plan],
            ["role", "role", "focused_sweep", "role", "role",
             "focused_sweep", "role", "role", "focused_sweep"])
        task_roles = [
            step for step in cfg.workflow_task
            if not hasattr(step, "action")]
        self.assertEqual(
            [(step.writes, step.produces_verdict)
             for step in task_roles],
            [(True, False), (False, True), (True, False)])
        self.assertEqual(
            [step.resolved_role.model for step in task_roles],
            ["lite", "prime", "lite"])
        self.assertEqual(
            [step.adapters for step in task_roles],
            [("codex", "claude"), ("claude", "codex"),
             ("codex", "claude")])
        plan_roles = [
            step for step in cfg.workflow_plan
            if not hasattr(step, "action")]
        self.assertEqual(
            [(step.writes, step.produces_verdict)
             for step in plan_roles],
            [(False, True), (True, False)] * 3)
        self.assertEqual(
            [step.model for step in plan_roles],
            ["prime", "lite"] * 3)
        self.assertEqual(
            [step.adapters for step in plan_roles],
            [("claude", "codex"), ("codex", "claude")] * 3)
        self.assertEqual(
            [step.action if hasattr(step, "action") else "role"
             for step in cfg.workflow_integration],
            ["full_verify", "role", "role", "full_verify"])
        integration_roles = cfg.workflow_integration[1:3]
        self.assertEqual(
            [(step.writes, step.produces_verdict)
             for step in integration_roles],
            [(False, True), (True, False)])
        self.assertEqual(
            [step.model for step in integration_roles],
            ["prime", "lite"])
        self.assertEqual(
            [step.adapters for step in integration_roles],
            [("claude", "codex"), ("codex", "claude")])


if __name__ == "__main__":
    unittest.main()
