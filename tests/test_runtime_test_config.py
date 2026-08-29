"""Configuration tests for the independent runtime-test workflow."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assent import AssentError
from assent.config import (BUILTIN_LAYER, PROJECT_LAYER, USER_LAYER,
                           WorkflowActionStep, WorkflowRoleStep, load_config)


MODEL_VALUES = {
    "claude": {
        "prime": "fable/high",
        "core": "opus/high",
        "lite": "sonnet/medium",
    },
    "codex": {
        "prime": "gpt-5.6-sol/high",
        "core": "gpt-5.6-terra/medium",
        "lite": "gpt-5.6-luna/low",
    },
}

ROLE_CONFIG = """
[abilities.writer]
prompt = "Write the fix."
writes = true
[abilities.reader]
prompt = "Inspect the candidate."
writes = false
[roles.writer]
ability = ["writer"]
model = "lite"
[roles.reader]
ability = ["reader"]
model = "core"
[roles.no_model]
ability = ["writer"]
"""


class TestRuntimeTestConfig(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.user_home = self.root / "user-assent"
        self.user_home.mkdir()
        self.environment = patch.dict(
            os.environ, {"ASSENT_HOME": str(self.user_home)}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def config_text(self, workflow: str, *, runtime_test: str = "",
                    adapters=("claude",), model_entries=None) -> str:
        model_entries = model_entries or MODEL_VALUES
        names = (json.dumps(list(adapters)) if len(adapters) != 1
                 else json.dumps(adapters[0]))
        text = f'[adapter]\nname = {names}\n' + ROLE_CONFIG
        for name, values in model_entries.items():
            text += f"\n[adapter.{name}.models]\n"
            text += "\n".join(f'{tier} = "{selection}"'
                               for tier, selection in values.items()) + "\n"
        if runtime_test:
            text += "\n" + runtime_test.strip() + "\n"
        return text + "\n" + workflow.strip() + "\n"

    def load(self, workflow: str,
             *, runtime_test: str = "", adapters=("claude",),
             model_entries=None):
        path = self.assent_dir / "assent.toml"
        path.write_text(
            self.config_text(workflow, runtime_test=runtime_test,
                             adapters=adapters, model_entries=model_entries),
            encoding="utf-8")
        return load_config(path, "plan01")

    def write_user(self, text: str) -> None:
        (self.user_home / "assent.toml").write_text(text, encoding="utf-8")

    def test_action_only_and_repair_arrays_are_resolved(self):
        cfg = self.load(
            """
[workflow]
task = [{ action = "focused_test" }]
runtime_test = [
    { action = "runtime_test" },
    { role = "writer", adapter = "codex", model = "prime" },
    { action = "runtime_test" },
    { role = "writer" },
    { action = "runtime_test" },
]
""", adapters=("claude", "codex"))

        self.assertEqual(cfg.workflow_runtime_test[0],
                         WorkflowActionStep("runtime_test"))
        self.assertIsInstance(cfg.workflow_runtime_test[1], WorkflowRoleStep)
        self.assertEqual(cfg.workflow_runtime_test[1].role, "writer")
        self.assertEqual(cfg.workflow_runtime_test[1].adapters, ("codex",))
        self.assertEqual(cfg.workflow_runtime_test[1].model, "prime")
        self.assertTrue(cfg.workflow_runtime_test[1].writes)
        self.assertEqual(cfg.workflow_runtime_test[3].adapters,
                         ("claude", "codex"))

        action_only = self.load(
            """
[workflow]
task = [{ action = "focused_test" }]
runtime_test = [{ action = "runtime_test" }]
""")
        self.assertEqual(action_only.workflow_runtime_test,
                         (WorkflowActionStep("runtime_test"),))

    def test_runtime_test_workflow_omission_stays_unset(self):
        cfg = self.load('[workflow]\ntask = [{ action = "focused_test" }]')
        self.assertIsNone(cfg.workflow_runtime_test)
        self.assertIsNone(cfg.runtime_test_commands)
        self.assertEqual(cfg.source_of("runtime_test.command"), BUILTIN_LAYER)

    def test_runtime_command_and_workflow_do_not_fallback_to_each_other(self):
        cfg = self.load(
            '[workflow]\ntask = [{ action = "focused_test" }]',
            runtime_test='[runtime_test]\ncommand = "run-project"')
        self.assertEqual(cfg.runtime_test_commands, ("run-project",))
        self.assertIsNone(cfg.workflow_runtime_test)

        cfg = self.load(
            """
[workflow]
task = [{ action = "focused_test" }]
runtime_test = [{ action = "runtime_test" }]
""")
        self.assertIsNone(cfg.runtime_test_commands)

    def test_runtime_command_keeps_project_and_user_provenance(self):
        self.write_user('[runtime_test]\ncommand = "user-command"\n')
        cfg = self.load(
            '[workflow]\ntask = [{ action = "focused_test" }]',
            runtime_test='[runtime_test]\ncommand = "project-command"')
        self.assertEqual(cfg.runtime_test_commands, ("project-command",))
        self.assertEqual(cfg.source_of("runtime_test.command"), PROJECT_LAYER)

        cfg = self.load('[workflow]\ntask = [{ action = "focused_test" }]')
        self.assertEqual(cfg.runtime_test_commands, ("user-command",))
        self.assertEqual(cfg.source_of("runtime_test.command"), USER_LAYER)

    def test_runtime_command_array_is_ordered_and_nonempty(self):
        cfg = self.load(
            '[workflow]\ntask = [{ action = "focused_test" }]',
            runtime_test=(
                '[runtime_test]\n'
                'command = ["first", "second", "third"]'))
        self.assertEqual(
            cfg.runtime_test_commands, ("first", "second", "third"))

        for value, message in (
                ('[]', "must not be empty"),
                ('["run", 1]', "array must contain only strings"),
                ('["run", "  "]', "runtime_test.command.*blank")):
            with self.subTest(value=value), self.assertRaisesRegex(
                    AssentError, message):
                self.load(
                    '[workflow]\ntask = [{ action = "focused_test" }]',
                    runtime_test=f'[runtime_test]\ncommand = {value}')

    def test_runtime_command_is_nonblank_and_runtime_section_is_closed(self):
        with self.assertRaisesRegex(AssentError, "runtime_test.command.*blank"):
            self.load(
                '[workflow]\ntask = [{ action = "focused_test" }]',
                runtime_test='[runtime_test]\ncommand = "   "')
        with self.assertRaisesRegex(AssentError, "unknown keys.*extra"):
            self.load(
                '[workflow]\ntask = [{ action = "focused_test" }]',
                runtime_test='[runtime_test]\ncommand = "run"\nextra = true')

    def test_runtime_test_structure_rejects_invalid_arrays(self):
        cases = (
            ("runtime_test = []", "must not be empty"),
            ('runtime_test = [{ role = "writer" }, '
             '{ action = "runtime_test" }]', "must start with an action"),
            ('runtime_test = [{ action = "runtime_test" }, '
             '{ role = "writer" }]', "must end with an action"),
            ('runtime_test = [{ action = "runtime_test" }, '
             '{ action = "runtime_test" }]', "strictly alternate"),
            ('runtime_test = [{ action = "runtime_test" }, '
             '{ role = "writer" }, { role = "writer" }, '
             '{ action = "runtime_test" }]', "strictly alternate"),
            ('runtime_test = [{ action = "runtime_test" }, '
             '{ role = "reader" }, { action = "runtime_test" }]',
             "must be writable"),
            ('runtime_test = [{ action = "not-runtime-test" }]',
             "unknown action"),
        )
        for entry, message in cases:
            with self.subTest(entry=entry), self.assertRaisesRegex(
                    AssentError, message):
                self.load(
                    '[workflow]\ntask = [{ action = "focused_test" }]\n'
                    + entry)

    def test_runtime_test_action_is_rejected_in_other_layers(self):
        for layer in ("task", "plan", "integration"):
            task = ('{0} = [{{ action = "runtime_test" }}]'
                    if layer == "task" else
                    'task = [{{ action = "focused_test" }}]\n'
                    '{0} = [{{ action = "runtime_test" }}]').format(layer)
            with self.subTest(layer=layer), self.assertRaisesRegex(
                    AssentError, rf"not valid under \[workflow\].{layer}"):
                self.load("[workflow]\n" + task)

    def test_runtime_test_role_requires_model_and_bound_adapter_tiers(self):
        with self.assertRaisesRegex(AssentError, "must state model"):
            self.load(
                """
[workflow]
task = [{ action = "focused_test" }]
runtime_test = [
    { action = "runtime_test" },
    { role = "no_model" },
    { action = "runtime_test" },
]
""")

        incomplete = {"claude": MODEL_VALUES["claude"],
                      "codex": {"core": "gpt-5.6-terra/medium"}}
        with self.assertRaisesRegex(AssentError, "every model tier"):
            self.load(
                """
[workflow]
task = [{ action = "focused_test" }]
runtime_test = [
    { action = "runtime_test" },
    { role = "writer", adapter = "codex", model = "core" },
    { action = "runtime_test" },
]
""", model_entries=incomplete)

    def test_runtime_test_literal_model_requires_one_adapter(self):
        with self.assertRaisesRegex(AssentError, "exactly one adapter"):
            self.load(
                """
[workflow]
task = [{ action = "focused_test" }]
runtime_test = [
    { action = "runtime_test" },
    { role = "writer", model = "vendor-model/high" },
    { action = "runtime_test" },
]
""", adapters=("claude", "codex"))

if __name__ == "__main__":
    unittest.main()
