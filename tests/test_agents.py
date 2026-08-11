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
        path = self.assent_dir / "assent.toml"
        path.write_text(text, encoding="utf-8")
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
effort = "heavy"
""")

        resolved = cfg.resolve_role("builder_reviewer")
        self.assertEqual(resolved.abilities,
                         (cfg.abilities["build"], cfg.abilities["review"]))
        self.assertEqual(resolved.model, "core")
        self.assertEqual(resolved.effort, "heavy")
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
            ("[abilities.build]\nprompt = \"x\"\nwrites = false\n"
             "[roles.worker]\nability = [\"build\"]\nmodel = \"max\"\n",
             "not a valid model tier"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = false\n"
             "[roles.worker]\nability = [\"build\"]\neffort = \"high\"\n",
             "not a valid effort"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(AssentError, message):
                self.load(text)

    def test_shipped_config_activates_default_roles_and_workflows(self):
        template = (Path(__file__).resolve().parents[1]
                    / "assent" / "templates" / "assent.toml")
        cfg = self.load(template.read_text(encoding="utf-8"))
        self.assertEqual(set(cfg.abilities),
                         {"write_tests", "implement_source", "review", "fix"})
        self.assertEqual(set(cfg.roles),
                         {"implementer", "test_writer", "source_implementer",
                          "reviewer", "fixer", "reviewer_fixer"})
        self.assertEqual(
            [step.action if hasattr(step, "action") else step.role
             for step in cfg.workflow_task],
            ["implementer", "focused_test"])
        self.assertEqual(
            [step.action if hasattr(step, "action") else step.role
             for step in cfg.workflow_plan],
            ["focused_sweep", "reviewer_fixer", "focused_sweep",
             "reviewer_fixer", "focused_sweep"])
        self.assertEqual(
            cfg.roles["implementer"].ability,
            ("write_tests", "implement_source"))
        self.assertEqual(cfg.roles["test_writer"].ability, ("write_tests",))
        self.assertEqual(cfg.roles["source_implementer"].ability,
                         ("implement_source",))
        self.assertEqual(cfg.roles["reviewer_fixer"].ability,
                         ("review", "fix"))
        self.assertEqual(
            [step.action if hasattr(step, "action") else step.role
             for step in cfg.workflow_integration],
            ["full_verify", "reviewer_fixer", "full_verify"])


if __name__ == "__main__":
    unittest.main()
