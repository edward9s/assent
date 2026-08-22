"""Preflight proves the concrete sessions in the linear arrays."""
import tempfile
import unittest
from pathlib import Path

from assent.adapters import Adapter
from assent.config import load_config
from assent.plan import Plan
from assent.preflight import capability_errors
from tests.engine_support import models_block, task_text


class RecordingAdapter(Adapter):
    def __init__(self):
        self.requests = ()

    def preflight(self, requests):
        self.requests = tuple(requests)
        return []


class TestPreflight(unittest.TestCase):
    def test_every_configured_task_role_is_preflighted(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        plan_dir = root / ".assent" / "plan01"
        plan_dir.mkdir(parents=True)
        (plan_dir / "t001_task.e.toml").write_text(
            task_text(), encoding="utf-8")
        text = """
[abilities.work]
prompt = "Work."
writes = true
[roles.first]
ability = ["work"]
model = "lite"
[roles.second]
ability = ["work"]
model = "core"
[workflow]
task = [{ role = "first" }, { role = "second" },
        { action = "focused_test" }]
"""
        config_path = root / ".assent" / "assent.toml"
        config_path.write_text(text + models_block(text), encoding="utf-8")
        cfg = load_config(config_path, "plan01")
        adapter = RecordingAdapter()
        self.assertEqual(capability_errors(
            cfg, adapter, Plan.parse(plan_dir), adapter_name="claude"), [])
        self.assertEqual(len(adapter.requests), 2)
        self.assertEqual([item.model for item in adapter.requests],
                         ["lite", "core"])


if __name__ == "__main__":
    unittest.main()
