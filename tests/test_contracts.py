"""Tests for the three shared contracts installed under ``~/.assent``.

Every filesystem case redirects ``ASSENT_HOME`` so the developer's real
contracts are never read or written.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError, contracts
from assent.config import load_config
from assent.plan import _KNOWN_KEYS
from assent.user_home import ASSENT_HOME_ENV


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestTestPackageIsolation(unittest.TestCase):
    def test_importing_tests_ignores_a_hostile_user_home(self):
        with tempfile.TemporaryDirectory() as hostile_directory:
            hostile_home = Path(hostile_directory)
            hostile_config = hostile_home / "assent.toml"
            hostile_text = "[hostile]\nsetting = true\n"
            hostile_config.write_text(hostile_text, encoding="utf-8")
            marker = hostile_home / "package-home.txt"
            environment = os.environ.copy()
            environment[ASSENT_HOME_ENV] = str(hostile_home)
            environment["ASSENT_TEST_HOME_MARKER"] = str(marker)
            script = """
import os
import tempfile
from pathlib import Path

import tests
from assent.config import load_config

home = Path(os.environ["ASSENT_HOME"])
with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    config = root / ".assent" / "assent.toml"
    config.parent.mkdir()
    config.write_text(
        'adapter.claude.models = { prime = "p/high", core = "c/high", '
        'lite = "l/low" }\\n'
        '[workflow]\\n'
        'task = [{ action = "focused_test" }]\\n',
        encoding="utf-8")
    loaded = load_config(config, "empty")
    if loaded.root != root.resolve():
        raise SystemExit(f"unexpected project root: {loaded.root}")
Path(os.environ["ASSENT_TEST_HOME_MARKER"]).write_text(
    str(home), encoding="utf-8")
"""
            result = subprocess.run(
                [sys.executable, "-c", script], cwd=_PROJECT_ROOT,
                env=environment, capture_output=True, encoding="utf-8",
                errors="replace")
            self.assertEqual(result.returncode, 0,
                             result.stdout + result.stderr)
            package_home = Path(marker.read_text(encoding="utf-8"))
            self.assertNotEqual(package_home, hostile_home)
            self.assertFalse(package_home.exists())
            self.assertEqual(hostile_config.read_text(encoding="utf-8"),
                             hostile_text)


def install_global_contracts(case: unittest.TestCase) -> Path:
    """Install current contracts in a temporary, redirected user home."""
    home = Path(tempfile.mkdtemp())
    case.addCleanup(shutil.rmtree, home, ignore_errors=True)
    environment = mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(home)})
    environment.start()
    case.addCleanup(environment.stop)
    for name in contracts.CONTRACT_NAMES:
        (home / name).write_text(contracts.installed_contract_text(name),
                                 encoding="utf-8", newline="\n")
    return home


class GlobalContractsMixin:
    """Give engine tests a current redirected contract installation."""

    def setUp(self):
        super().setUp()
        self.user_home = install_global_contracts(self)


class TestContractPaths(unittest.TestCase):
    def test_paths_resolve_under_the_redirected_user_home(self):
        home = install_global_contracts(self)
        self.assertEqual(contracts.contract_dir(), home)
        self.assertEqual(contracts.instructions_path(),
                         home / "instructions.md")
        self.assertEqual(contracts.contract_path("format.md"),
                         home / "format.md")

    def test_an_unknown_contract_name_is_refused(self):
        install_global_contracts(self)
        with self.assertRaises(AssentError) as ctx:
            contracts.contract_path("AGENTS.md")
        self.assertIn("unknown global contract", str(ctx.exception))

    def test_installed_text_comes_from_packaged_templates(self):
        install_global_contracts(self)
        packaged = Path(contracts.__file__).resolve().parent / "templates"
        for name in contracts.CONTRACT_NAMES:
            with self.subTest(contract=name):
                self.assertEqual(
                    contracts.installed_contract_text(name),
                    (packaged / name).read_text(encoding="utf-8"))




class TestContractValidation(unittest.TestCase):
    def test_current_contracts_report_no_error(self):
        install_global_contracts(self)
        self.assertEqual(contracts.contract_errors(), [])
        contracts.require_contracts()

    def test_windows_line_endings_stay_current(self):
        home = install_global_contracts(self)
        path = home / "instructions.md"
        path.write_bytes(path.read_text(encoding="utf-8").replace(
            "\n", "\r\n").encode("utf-8"))
        self.assertEqual(contracts.contract_errors(), [])

    def test_missing_contract_is_named(self):
        home = install_global_contracts(self)
        (home / "format.md").unlink()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn(str(home / "format.md"), errors[0])
        self.assertIn("missing", errors[0])

    def test_empty_user_home_names_every_contract(self):
        home = install_global_contracts(self)
        for name in contracts.CONTRACT_NAMES:
            (home / name).unlink()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), len(contracts.CONTRACT_NAMES))
        for name, error in zip(contracts.CONTRACT_NAMES, errors):
            with self.subTest(contract=name):
                self.assertIn(str(home / name), error)

    def test_stale_contract_is_reported(self):
        home = install_global_contracts(self)
        (home / "instructions.md").write_text(
            "older working instructions\n", encoding="utf-8")
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("stale", errors[0])
        self.assertIn("instructions.md", errors[0])

    def test_unreadable_contract_is_reported_not_raised(self):
        home = install_global_contracts(self)
        (home / "format.md").unlink()
        (home / "format.md").mkdir()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be read", errors[0])

    def test_require_contracts_fails_closed_with_init_remedy(self):
        home = install_global_contracts(self)
        (home / "instructions.md").unlink()
        with self.assertRaises(AssentError) as ctx:
            contracts.require_contracts()
        message = str(ctx.exception)
        self.assertIn(str(home / "instructions.md"), message)
        self.assertIn("assent init", message)


class TestContractContent(unittest.TestCase):
    def test_plan_format_requires_an_explicit_runtime_contract(self):
        text = (_PROJECT_ROOT / "assent/templates/format.md").read_text(
            encoding="utf-8")
        compact = " ".join(text.split())
        for phrase in (
                "_runtime_test.toml",
                'execution = "disabled"',
                'execution = "explicit"',
                'execution = "after_plan"',
                "one non-empty string or a non-empty array",
                "Array order is execution order",
                "Every live plan contains exactly one",
                "There is no fallback, alias, migration",
                "including when it selects `disabled`",
                "Unknown never means `disabled`"):
            with self.subTest(phrase=phrase):
                self.assertIn(" ".join(phrase.split()), compact)
        self.assertNotIn("[runtime_test].command", text)
        self.assertNotIn("project command", text)

    def test_planning_instructions_configure_both_verification_gates(self):
        text = (_PROJECT_ROOT / "assent/templates/instructions.md").read_text(
            encoding="utf-8")
        compact = " ".join(text.split())
        for phrase in (
                "Before a planning meeting finishes a live plan",
                "configures the project-owned command block",
                "greatest safe parallelism",
                "run_unittest_parallel()",
                "chooses exactly one runtime execution mode",
                "never because the command is unknown",
                "ask the human rather than guessing",
                "Keep the task file's ten-field schema",
                "never become task fields"):
            with self.subTest(phrase=phrase):
                self.assertIn(" ".join(phrase.split()), compact)

    def test_runtime_repair_defaults_are_global(self):
        config_text = (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
            encoding="utf-8")
        data = tomllib.loads(config_text)
        ability = data["abilities"]["runtime_repair"]
        self.assertTrue(ability["writes"])
        for phrase in (
                "ordinary project source", "tests", "fixtures",
                "project configuration", "documentation", "Do not run any command",
                "task contracts", "Git state", "control state",
                "declare the runtime test passed"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, ability["prompt"])

        self.assertEqual(data["roles"]["runtime_repairer"]["ability"],
                         ["runtime_repair"])
        self.assertEqual(data["roles"]["runtime_repairer"]["model"], "core")
        self.assertNotIn("runtime_test", data["workflow"])
        self.assertNotIn("[runtime_test]", config_text)

    def test_workflow_contract_states_the_three_governing_principles(self):
        text = (_PROJECT_ROOT / "assent/templates/workflow.md").read_text(
            encoding="utf-8")
        self.assertIn("Reliability by construction", text)
        self.assertIn("Semantic precision", text)
        self.assertIn("first authoritative", text)
        self.assertIn("never masked", text)
        self.assertIn("one finite linear interpreter", text)
        self.assertIn("There is no finding ledger", text)

    def test_task_contract_has_no_scope_or_verdict_protocol(self):
        format_text = (_PROJECT_ROOT / "assent/templates/format.md").read_text(
            encoding="utf-8")
        workflow = (_PROJECT_ROOT / "assent/templates/workflow.md").read_text(
            encoding="utf-8")
        config = (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
            encoding="utf-8")
        self.assertNotIn("scope =", format_text)
        self.assertNotIn("produces_verdict", config)
        self.assertNotIn("_auto_fix.toml", workflow)

    def test_session_contract_keeps_control_state_scheduler_owned(self):
        text = (_PROJECT_ROOT / "assent/templates/instructions.md").read_text(
            encoding="utf-8")
        self.assertIn("scheduler owns", text.lower())
        self.assertIn("task contracts, journals,", text)
        self.assertIn("scheduler state", text)
        self.assertIn("smallest architecture and fewest states", text)
        self.assertIn("one actual mechanism", text)
        self.assertIn("first authoritative", text)
        self.assertIn("do not create a backward-", text)

    def test_every_writable_repair_ability_fixes_tests_or_implementation(self):
        data = tomllib.loads(
            (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
                encoding="utf-8"))
        for name in ("task_fix", "plan_fix", "integration_fix"):
            with self.subTest(ability=name):
                prompt = data["abilities"][name]["prompt"]
                self.assertIn("authoritative requirements", prompt)
                self.assertIn("tests or the implementation", prompt)
                self.assertIn("Preserve correct tests", prompt)
                self.assertIn("never weaken", prompt)

    def test_source_and_repair_abilities_apply_governing_principles(self):
        data = tomllib.loads(
            (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
                encoding="utf-8"))
        names = (
            "implement_source",
            "task_review",
            "task_fix",
            "plan_quality_review",
            "plan_review",
            "plan_fix",
            "integration_review",
            "integration_fix",
        )
        for name in names:
            with self.subTest(ability=name):
                prompt = data["abilities"][name]["prompt"]
                self.assertIn("smallest architecture and fewest states", prompt)
                self.assertIn("one term per actual mechanism", prompt)
                self.assertIn("first authoritative boundary", prompt)
                self.assertIn("compatibility or masking", prompt)

    def test_integration_contract_automates_typed_conflicts(self):
        text = " ".join(
            (_PROJECT_ROOT / "assent/templates/workflow.md").read_text(
                encoding="utf-8").split())
        self.assertIn("managed reconcile worktree", text)
        self.assertIn("persistent source worktree", text)
        self.assertIn("candidate reconstruction, and recheck", text)
        self.assertIn("without mechanically identified source attribution", text)

    def test_runtime_workflow_contract_covers_targets_boundaries_and_receipts(self):
        text = " ".join(
            (_PROJECT_ROOT / "assent/templates/workflow.md").read_text(
                encoding="utf-8").split())
        for phrase in (
                "`assent test [PLAN]`", "project-layer `[runtime_test].command`",
                "plan candidate worktree", "current primary working tree",
                "after the plan workflow and before the selection's integration `full_verify`",
                "exit 0 records `PASSED`", "source or command-list drift records `STALE`",
                "makes no working-tree source change", "quota waits", "restart",
                "REVIEW UNRESOLVED, HUMAN DECISION", "Runtime evidence is not a verification receipt",
                "acceptance requires both fresh receipt evidence"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
