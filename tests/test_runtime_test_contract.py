"""Tests for the per-plan runtime-test contract."""
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from assent import AssentError
from assent.plan import (
    RUNTIME_TEST_CONTRACT_NAME,
    RuntimeTestContract,
    parse_runtime_test_contract,
)


class TestRuntimeTestContract(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plan_dir = Path(temporary.name)

    def write_contract(self, text: str) -> Path:
        path = self.plan_dir / RUNTIME_TEST_CONTRACT_NAME
        path.write_text(text, encoding="utf-8")
        return path

    def assert_rejected(self, text: str, reason: str):
        self.write_contract(text)
        with self.assertRaises(AssentError) as context:
            parse_runtime_test_contract(self.plan_dir)
        message = str(context.exception)
        self.assertIn(RUNTIME_TEST_CONTRACT_NAME, message)
        self.assertIn(reason, message)

    def test_disabled_has_no_command_value(self):
        self.write_contract('execution = "disabled"\n')

        contract = parse_runtime_test_contract(self.plan_dir)

        self.assertEqual(contract, RuntimeTestContract("disabled", None))
        self.assertIsNone(contract.commands)

    def test_explicit_preserves_command_value(self):
        command = "  python -m unittest tests.test_runtime_test_contract  "
        self.write_contract(
            f'execution = "explicit"\ncommand = {json.dumps(command)}\n')

        contract = parse_runtime_test_contract(self.plan_dir)

        self.assertEqual(contract.execution, "explicit")
        self.assertEqual(contract.commands, (command,))

    def test_after_plan_requires_and_returns_command(self):
        self.write_contract(
            'execution = "after_plan"\ncommand = "python -m unittest"\n')

        self.assertEqual(
            parse_runtime_test_contract(self.plan_dir),
            RuntimeTestContract("after_plan", ("python -m unittest",)),
        )

    def test_command_array_preserves_order(self):
        self.write_contract(
            'execution = "explicit"\n'
            'command = ["first", "second", "third"]\n')

        self.assertEqual(
            parse_runtime_test_contract(self.plan_dir).commands,
            ("first", "second", "third"))

    def test_contract_is_immutable(self):
        self.write_contract('execution = "disabled"\n')
        contract = parse_runtime_test_contract(self.plan_dir)

        with self.assertRaises(FrozenInstanceError):
            contract.execution = "explicit"

    def test_missing_contract_is_rejected_without_task_verify_fallback(self):
        (self.plan_dir / "t001_task.e.toml").write_text(
            'verify = "python -m unittest"\n', encoding="utf-8")

        with self.assertRaises(AssentError) as context:
            parse_runtime_test_contract(self.plan_dir)

        message = str(context.exception)
        self.assertIn(RUNTIME_TEST_CONTRACT_NAME, message)
        self.assertIn("missing", message)

    def test_unreadable_contract_is_rejected(self):
        (self.plan_dir / RUNTIME_TEST_CONTRACT_NAME).mkdir()

        with self.assertRaises(AssentError) as context:
            parse_runtime_test_contract(self.plan_dir)

        message = str(context.exception)
        self.assertIn(RUNTIME_TEST_CONTRACT_NAME, message)
        self.assertIn("Cannot read", message)

    def test_invalid_toml_is_rejected(self):
        self.assert_rejected('execution = "explicit"\ncommand =\n', "not valid TOML")

    def test_unknown_keys_are_rejected(self):
        self.assert_rejected(
            'execution = "disabled"\nextra = true\n', "undefined fields: extra")

    def test_missing_execution_is_rejected(self):
        self.assert_rejected('command = "python -m unittest"\n', "missing required field: execution")

    def test_execution_type_is_rejected(self):
        self.assert_rejected("execution = true\n", "field execution must be a string")

    def test_unknown_execution_values_are_not_normalized_or_aliased(self):
        for execution in ("EXPLICIT", "enabled"):
            with self.subTest(execution=execution):
                self.assert_rejected(
                    f'execution = "{execution}"\ncommand = "python -m unittest"\n',
                    f"unknown execution {execution!r}",
                )

    def test_disabled_rejects_command(self):
        self.assert_rejected(
            'execution = "disabled"\ncommand = "python -m unittest"\n',
            "must not define command",
        )

    def test_enabled_modes_require_command(self):
        for execution in ("explicit", "after_plan"):
            with self.subTest(execution=execution):
                self.assert_rejected(
                    f'execution = "{execution}"\n', "missing required field: command")

    def test_command_type_is_rejected(self):
        self.assert_rejected(
            'execution = "explicit"\ncommand = 1\n',
            "field command must be a string or an array of strings",
        )

    def test_command_array_rejects_empty_non_string_and_blank_entries(self):
        self.assert_rejected(
            'execution = "explicit"\ncommand = []\n',
            "field command must not be empty")
        self.assert_rejected(
            'execution = "explicit"\ncommand = ["run", 1]\n',
            "command array must contain only strings")
        self.assert_rejected(
            'execution = "explicit"\ncommand = ["run", "  "]\n',
            "field command[1] must not be empty or whitespace")

    def test_blank_commands_are_rejected(self):
        for command in ("", "   ", "\t"):
            with self.subTest(command=repr(command)):
                toml_command = command.replace("\\", "\\\\").replace('"', '\\"')
                self.assert_rejected(
                    f'execution = "after_plan"\ncommand = "{toml_command}"\n',
                    "must not be empty or whitespace",
                )


if __name__ == "__main__":
    unittest.main()
