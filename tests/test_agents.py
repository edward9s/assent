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


class AgentConfigTestCase(unittest.TestCase):
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


class TestAgents(AgentConfigTestCase):
    def test_composed_role_resolves_in_order_and_derives_flags(self):
        cfg = self.load("""
[abilities.build]
prompt = "Implement the requested change."
writes = true
gate = false

[abilities.review]
prompt = "Return a scheduler verdict."
writes = false
gate = true
produces_verdict = true

[agents.builder_reviewer]
ability = ["build", "review"]
model = "core"
effort = "heavy"
""")

        resolved = cfg.resolve_agent("builder_reviewer")
        self.assertEqual(resolved.abilities,
                         (cfg.abilities["build"], cfg.abilities["review"]))
        self.assertEqual(resolved.model, "core")
        self.assertEqual(resolved.effort, "heavy")
        self.assertTrue(resolved.writes)
        self.assertTrue(resolved.gate)
        self.assertTrue(resolved.produces_verdict)

    def test_review_name_does_not_imply_a_verdict(self):
        cfg = self.load("""
[abilities.review]
prompt = "Inspect without a scheduler verdict."
writes = false
gate = false
produces_verdict = false

[agents.reader]
ability = ["review"]
""")

        resolved = cfg.resolve_agent("reader")
        self.assertFalse(resolved.writes)
        self.assertFalse(resolved.gate)
        self.assertFalse(resolved.produces_verdict)

    def test_missing_ability_and_empty_role_refuse_to_load(self):
        with self.assertRaisesRegex(AssentError, "missing ability 'absent'"):
            self.load("[agents.worker]\nability = [\"absent\"]\n")
        with self.assertRaisesRegex(AssentError, "non-empty array"):
            self.load("[agents.worker]\nability = []\n")

    def test_wrong_types_unknown_keys_and_invalid_tiers_refuse_to_load(self):
        cases = (
            ("[abilities.build]\nprompt = 1\nwrites = true\ngate = false\n",
             "wrong type"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = true\ngate = false\nextra = 1\n",
             "unknown keys"),
            ("[agents.worker]\nability = [1]\n", "all-string"),
            ("[agents.worker]\nability = [\"build\"]\nextra = 1\n",
             "unknown keys"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = false\ngate = false\n"
             "[agents.worker]\nability = [\"build\"]\nmodel = \"max\"\n",
             "not a valid model tier"),
            ("[abilities.build]\nprompt = \"x\"\nwrites = false\ngate = false\n"
             "[agents.worker]\nability = [\"build\"]\neffort = \"high\"\n",
             "not a valid effort"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(AssentError, message):
                self.load(text)

    def test_existing_config_fixture_loads_with_empty_tables(self):
        template = (Path(__file__).resolve().parents[1]
                    / "assent" / "templates" / "assent.toml")
        cfg = self.load(template.read_text(encoding="utf-8"))
        self.assertEqual(cfg.abilities, {})
        self.assertEqual(cfg.agents, {})


if __name__ == "__main__":
    unittest.main()
