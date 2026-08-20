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
from dataclasses import fields
from pathlib import Path
from unittest import mock

from assent import AssentError, auto_fix, contracts
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
        'lite = "l/low" }',
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


class TestContractContent(unittest.TestCase):
    """Check durable behavior, not incidental prose or internal field lists."""

    @staticmethod
    def _compact(name: str) -> str:
        return " ".join(contracts.installed_contract_text(name).split())

    def test_task_skeleton_matches_all_parser_fields(self):
        format_text = contracts.installed_contract_text("format.md")
        match = re.search(
            r'```toml\n(title = "Skeleton and test infrastructure".*?\n)```',
            format_text, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        skeleton = tomllib.loads(match.group(1))
        self.assertEqual(len(_KNOWN_KEYS), 11)
        self.assertEqual(set(skeleton), _KNOWN_KEYS)

    def test_contracts_assign_one_owner_and_minimal_reading_scope(self):
        instructions = self._compact("instructions.md")
        for phrase in (
                "repository-specific development constraints belong in `AGENTS.md`",
                "scheduled-session procedure belongs in `instructions.md`",
                "persisted artifact schemas, filename rules, and state meanings belong in `format.md`",
                "CLI, report, and receipt contracts belong in `workflow.md`",
                "An **assent-scheduled task session** reads only"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)
        scheduled = instructions.split(
            "An **assent-scheduled task session** reads only:", 1)[1].split(
                "## Scheduled-session rules", 1)[0]
        self.assertIn("its one task file", scheduled)
        self.assertNotIn("format.md", scheduled)
        self.assertNotIn("workflow.md", scheduled)

    def test_plan_format_requires_complete_scope_and_narrow_verification(self):
        format_text = self._compact("format.md")
        for phrase in (
                "audit every `goal`, `behavior`, and `acceptance` clause item by item",
                "Cover every possible write with an exact `scope` entry",
                "smallest module, class, case, or command",
                "leave no non-ignored worktree change",
                "Audit every `verify` command against the clean-worktree rule",
                "Never name `.assent/verify.py` or the full suite",
                "A task must be executable by a fresh AI"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, format_text)

    def test_the_commented_split_workflow_still_loads_when_swapped_in(self):
        """The shipped alternative is commented out, so nothing else would catch it rotting.

        A commented example cannot be parsed, validated, or refactored with the rest of
        the file, so a role rename or a workflow rule change would leave it quietly
        wrong for whoever pastes it in.  Uncommenting it here keeps it honest: it has to
        name live roles and satisfy the same workflow validation as the active form.
        """
        templates = _PROJECT_ROOT / "assent" / "templates"
        text = (templates / "assent.toml").read_text(encoding="utf-8")

        swapped, dropping = [], False
        for line in text.splitlines():
            stripped = line.strip()
            if re.match(r"^(task|plan|integration) = \[$", stripped):
                dropping = True
                continue
            if dropping:
                dropping = stripped != "]"
                continue
            if re.match(r"^# ((task|plan|integration) = \[|  \{|\])", stripped):
                swapped.append(stripped[2:] if stripped.startswith("# ")
                               else stripped[1:])
                continue
            swapped.append(line)
        rebuilt = "\n".join(swapped) + "\n"
        self.assertNotEqual(rebuilt, text)   # the swap actually changed something

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assent_dir = root / ".assent"
            (assent_dir / "plan01").mkdir(parents=True)
            (assent_dir / "assent.toml").write_text(rebuilt, encoding="utf-8")
            shutil.copy(templates / "adapter.toml", assent_dir / "adapter.toml")
            with mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(root / "home")}):
                cfg = load_config(assent_dir / "assent.toml", "plan01")

        self.assertEqual(
            [step.action if hasattr(step, "action") else step.role
             for step in cfg.workflow_task],
            ["tests_writer", "source_implementer", "focused_test",
             "task_reviewer", "task_fixer", "focused_test"])
        self.assertEqual(
            [step.action if hasattr(step, "action") else step.role
             for step in cfg.workflow_integration],
            ["full_verify", "integration_reviewer", "integration_fixer",
             "full_verify"])
        # The split form is the one that keeps every verdict out of a writing session.
        self.assertEqual(
            [(step.writes, step.produces_verdict)
             for step in cfg.workflow_plan if not hasattr(step, "action")],
            [(False, True), (True, False)] * 3)

    def test_workflow_layers_match_the_default_configuration(self):
        workflow = self._compact("workflow.md")
        for phrase in (
                "`[workflow]` has exactly three ordered arrays",
                "`focused_test` is legal only at task positions",
                "`focused_sweep` is legal only at plan positions",
                "`full_verify` is legal only at integration positions",
                "exit 0 with any non-ignored worktree change is `STALE` evidence",
                "The engine never infers behavior from a role or ability name"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, workflow)

        configuration = tomllib.loads(
            (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
                encoding="utf-8"))
        self.assertEqual(
            [step.get("action", "role")
             for step in configuration["workflow"]["task"]],
            ["role", "focused_test", "role", "focused_test"])
        plan_shape = [
            {"action": step["action"]} if "action" in step else {"role": "role"}
            for step in configuration["workflow"]["plan"]]
        self.assertEqual(
            plan_shape,
            [{"role": "role"},
             {"action": "focused_sweep"}, {"role": "role"},
             {"action": "focused_sweep"}, {"role": "role"},
             {"action": "focused_sweep"}])
        integration_shape = [
            {"action": step["action"]} if "action" in step else {"role": "role"}
            for step in configuration["workflow"]["integration"]]
        self.assertEqual(
            integration_shape,
            [{"action": "full_verify"}, {"role": "role"},
             {"action": "full_verify"}])
        self.assertIn("current task",
                      configuration["abilities"]["task_review"]["prompt"])
        self.assertIn("cumulative worktree",
                      configuration["abilities"]["plan_review"]["prompt"])
        self.assertIn(
            "exact selection",
            configuration["abilities"]["integration_review"]["prompt"])

    def test_scheduled_session_contract_owns_command_side_effects(self):
        instructions = self._compact("instructions.md")
        self.assertIn("Command side effects count as writes", instructions)
        self.assertIn(
            "never run a scheduler-owned `focused_test` action", instructions)
        self.assertIn(
            "Uncommitted primary-tree changes to files such as `AGENTS.md` and `.gitignore` are not inherited",
            instructions)

    def test_default_write_abilities_resist_representation_coupled_tests(self):
        configuration = tomllib.loads(
            (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
                encoding="utf-8"))
        writable_prompts = [
            ability["prompt"]
            for ability in configuration["abilities"].values()
            if ability["writes"]]

        # Prompt text is the shipped behavior under test here. Ability names are
        # deliberately irrelevant: all default writers share the safety floor.
        for prompt in writable_prompts:
            with self.subTest(prompt=prompt):
                self.assertIn("do not weaken", prompt.lower())

        verdict_prompts = [
            ability["prompt"]
            for ability in configuration["abilities"].values()
            if ability.get("produces_verdict", False)]
        for prompt in verdict_prompts:
            with self.subTest(prompt=prompt):
                self.assertIn(
                    "do not accept tests that merely mirror", prompt.lower())
                self.assertIn("proving the cited requirement", prompt.lower())

        semantic_test_prompts = [
            prompt for prompt in writable_prompts
            if "tests that prove" in prompt]
        self.assertEqual(len(semantic_test_prompts), 1)
        test_prompt = semantic_test_prompts[0]
        for evidence in (
                "observable semantics and invariants",
                "unless a requirement explicitly makes them public",
                "an alternate valid combination",
                "a semantics-preserving input transformation",
                "rejection of an invalid combination",
                "rather than a packaged example"):
            with self.subTest(evidence=evidence):
                self.assertIn(evidence, test_prompt)

    def test_task_plan_and_integration_responsibilities_are_distinct(self):
        workflow = self._compact("workflow.md")
        for phrase in (
                "If its first `focused_test` passes, the layer completes and skips repair",
                "A role that self-marks `BLOCKED` advances to the next task verdict role",
                "repairs that path in the same session",
                "Task failure never consumes a plan review position",
                "The plan workflow is considered only after every task is `DONE` or `SKIP`",
                "cumulative implementation conforms to the plan",
                "reconstructs the same exact snapshotted selection",
                "never asks to skip, silently removes a plan, accepts a compatible prefix"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, workflow)

    def test_verification_and_acceptance_boundaries_are_explicit(self):
        instructions = self._compact("instructions.md")
        workflow = self._compact("workflow.md")
        common_instructions = instructions.split(
            "## Scheduled-session rules", 1)[0]
        self.assertIn(
            "An AI session never initiates the full suite or "
            "`.assent/verify.py`",
            common_instructions)
        self.assertIn(
            "the scheduler owns any workflow `full_verify` action and runs it "
            "outside the AI session",
            common_instructions)
        for phrase in (
                "Only the explicit human `assent accept` command publishes work",
                "Focused verification runs task commands in source worktrees, writes no receipt",
                "Complete verification builds a temporary integration candidate",
                "Direct `accept PLAN` and selected `accept A B` never start verification"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, workflow)

    def test_unresolved_review_and_recovery_preserve_work(self):
        workflow = self._compact("workflow.md")
        for phrase in (
                "Failure, interruption, and repair never revert the workspace automatically",
                "REVIEW UNRESOLVED, HUMAN DECISION",
                "exits zero so unrelated queued plans continue",
                "`NEEDS_REPAIR`, `REPAIRING`, `AWAITING_REVIEW`, or `COMPLETE`",
                "resets only the orchestration cursor",
                "re-adjudicates them from the first position of the current workflow"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, workflow)
        self.assertEqual(auto_fix.AUTO_FIX_STATE_VERSION, 7)
        self.assertEqual({field.name for field in fields(auto_fix.AutoFixState)},
                         auto_fix._STATE_KEYS)

    def test_manual_rework_and_reject_have_distinct_boundaries(self):
        workflow = self._compact("workflow.md")
        self.assertIn("`reject` is an explicitly confirmed destructive reset",
                      workflow)
        self.assertIn("resets `DONE`, `WIP`, and `BLOCKED` tasks to `TODO`",
                      workflow)
        self.assertIn("`rework` reopens existing tasks while preserving code",
                      workflow)

    def test_shared_input_and_cleanup_rules_are_available_to_sessions(self):
        instructions = self._compact("instructions.md")
        workflow = self._compact("workflow.md")
        for phrase in (
                "runs the injected `assent shared-paths review` command",
                "Cover every listed ordinary ignored directory once",
                "Never hand-create a source-worktree link",
                "Never copy an ignored tree",
                "modify a linked target"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)
        for phrase in (
                "`NO-IGNORED-DIRECTORY-CANDIDATE`",
                "undeclared manual links refuse verification",
                "never traverses its resolved target"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, workflow)

    def test_reader_recovery_never_recommends_raw_worktree_removal(self):
        paths = (
            _PROJECT_ROOT / "AGENTS.md",
            _PROJECT_ROOT / "assent/templates/format.md",
            _PROJECT_ROOT / "README.md",
            _PROJECT_ROOT / "README.zh-TW.md",
        )
        for path in paths:
            with self.subTest(path=path):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("git worktree remove", text)
                self.assertNotIn("Remove residue manually", text)


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


if __name__ == "__main__":
    unittest.main()
