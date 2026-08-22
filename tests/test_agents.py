"""Ability and role configuration has no hidden semantic names."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.config import load_config
from assent.user_home import ASSENT_HOME_ENV
from tests.engine_support import models_block


class TestRoles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / ".assent").mkdir()
        home = self.root / "home"
        home.mkdir()
        environment = mock.patch.dict(
            os.environ, {ASSENT_HOME_ENV: str(home)})
        environment.start()
        self.addCleanup(environment.stop)

    def load(self, text: str):
        path = self.root / ".assent" / "assent.toml"
        text += '\n[workflow]\ntask = [{ action = "focused_test" }]\n'
        path.write_text(text + models_block(text), encoding="utf-8")
        return load_config(path, "plan01")

    def test_role_composes_abilities_in_order(self):
        cfg = self.load("""
[abilities.repair]
prompt = "Review and repair the candidate simply."
writes = true
[abilities.explain]
prompt = "Explain the resulting design."
writes = true
[roles.repairer]
ability = ["repair", "explain"]
model = "core"
""")
        role = cfg.resolve_role("repairer")
        self.assertEqual(
            [ability.prompt for ability in role.abilities],
            ["Review and repair the candidate simply.",
             "Explain the resulting design."])
        self.assertTrue(role.writes)
        self.assertEqual(role.model, "core")

    def test_role_name_does_not_imply_behavior(self):
        cfg = self.load("""
[abilities.observe]
prompt = "Inspect only."
writes = false
[roles.fixer]
ability = ["observe"]
""")
        self.assertFalse(cfg.resolve_role("fixer").writes)

    def test_structured_verdict_key_is_not_part_of_the_schema(self):
        with self.assertRaisesRegex(AssentError, "unknown keys: produces_verdict"):
            self.load("""
[abilities.review]
prompt = "Inspect."
writes = false
produces_verdict = true
""")

    def test_combined_review_and_repair_role_is_writable(self):
        cfg = self.load("""
[abilities.read]
prompt = "Inspect."
writes = false
[abilities.write]
prompt = "Repair."
writes = true
[roles.mixed]
ability = ["read", "write"]
""")
        self.assertTrue(cfg.resolve_role("mixed").writes)


if __name__ == "__main__":
    unittest.main()
