"""contracts tests: the global ~/.assent/instructions.md and format.md gate.

Every case redirects the user home with ASSENT_HOME, so the developer's real
~/.assent is never read or written.  ``install_global_contracts`` is the fixture
the CLI, engine and inspection test modules share to put a temporary user home
with current contracts in place.
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
    config.write_text("", encoding="utf-8")
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
    """Point ASSENT_HOME at a temporary user home holding the current contracts.

    Returns that directory so a test can then delete or edit one contract to
    produce the missing/stale cases.  Cleanup is registered on the test case.
    """
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
    """Give a test case current global contracts under a temporary user home.

    ``run`` and ``check`` now fail closed without them, so every case that drives
    either one mixes this in ahead of its own base class.  It deliberately stays a
    plain mixin -- it must compose with the git-backed engine fixtures without this
    module having to import them.
    """

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

    def test_the_installed_text_comes_from_the_packaged_templates(self):
        install_global_contracts(self)
        packaged = Path(contracts.__file__).resolve().parent / "templates"
        for name in contracts.CONTRACT_NAMES:
            with self.subTest(contract=name):
                self.assertEqual(
                    contracts.installed_contract_text(name),
                    (packaged / name).read_text(encoding="utf-8"))


class TestContractContent(unittest.TestCase):
    """Durable rules a reader must be able to find in the shipped contract."""

    def test_task_skeleton_matches_all_twelve_parser_fields(self):
        format_text = contracts.installed_contract_text("format.md")
        match = re.search(
            r'```toml\n(title = "Skeleton and test infrastructure".*?\n)```',
            format_text, re.DOTALL)
        self.assertIsNotNone(match)
        skeleton = tomllib.loads(match.group(1))

        self.assertEqual(len(_KNOWN_KEYS), 12)
        self.assertEqual(set(skeleton), _KNOWN_KEYS)
        self.assertIn("workflow", skeleton)

    def test_planning_contract_requires_owner_scope_audit_and_narrow_gates(self):
        text = " ".join(contracts.installed_contract_text("format.md").split())
        for phrase in (
                "audit every `goal`, `behavior`, and `acceptance` clause item by item",
                "owning implementation, focused-test, and contract files",
                "read-only context or a possible write",
                "covered by an exact `scope` entry",
                "inspect the repository",
                "not a completeness proof",
                "module, class, case, or command",
                "whole high-I/O module",
                "smallest representative integration test"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_workflow_contract_and_template_define_the_three_execution_layers(self):
        workflow = " ".join(
            contracts.installed_contract_text("workflow.md").split())
        for phrase in (
                "`task` selects task-scoped context",
                "`plan` selects plan-scoped context",
                "[workflow].integration",
                "exactly three keys",
                "tagged union",
                "`focused_sweep` is legal only at plan positions",
                "`full_verify` is legal only at integration positions",
                "not permission",
                "The selected role's `[abilities]` carry what that session does",
                "The engine never infers behavior from a role or ability name",
                "`produces_verdict`",
                "makes the whole plan one unit",
                "every `plan` role step is an ordinary worker session",
                "according to the plan's focused gate",
                "The plan workflow is considered only after every task",
                "never consumes a plan review position",
                "self-marks `BLOCKED` advances to the next verdict role",
                "non-empty `integration` must start and end with `full_verify`",
                "A writable verdict role reviews and repairs either failure in one session",
                "A writable verdict role that finds one exact mechanically valid scope omission repairs",
                "the first role after `full_verify` must produce a verdict",
                "Neither form repeats a successful complete verification"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, workflow)
        self.assertNotIn("exactly two keys", workflow)

        configuration = (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
            encoding="utf-8")
        adapter_configuration = (
            _PROJECT_ROOT / "assent/templates/adapter.toml").read_text(
                encoding="utf-8")
        self.assertNotIn("[auto_fix.review]", configuration)
        self.assertNotIn("reviewer round", configuration.lower())
        self.assertNotIn("[adapter]", configuration)
        self.assertIn("[adapter]", adapter_configuration)
        for phrase in ("[abilities.write_tests]", "[abilities.implement_source]",
                       "[abilities.review]", "[abilities.fix]",
                       "[abilities.task_review]", "[abilities.task_fix]",
                       "[abilities.plan_review]", "[abilities.plan_fix]",
                       "[roles.implementer]", "[roles.test_writer]",
                       "[roles.source_implementer]", "[roles.reviewer_fixer]",
                       "[roles.task_reviewer_fixer]",
                       "[roles.plan_reviewer_fixer]",
                       "[workflow]", "task =", "plan =", "integration =",
                       "# focused_sweep is legal only in workflow.plan",
                       "# focused_test is legal only in workflow.task"):
            with self.subTest(template_phrase=phrase):
                self.assertIn(phrase, configuration)
        parsed_configuration = tomllib.loads(configuration)
        self.assertNotIn("agents", parsed_configuration)
        self.assertIn("roles", parsed_configuration)
        self.assertEqual(parsed_configuration["workflow"]["task"],
                         [{"role": "implementer"},
                          {"action": "focused_test"},
                          {"role": "task_reviewer_fixer"},
                          {"action": "focused_test"}])
        for split_step in (
                '#   { role = "test_writer" },',
                '#   { role = "source_implementer" },',
                '#   { action = "focused_test" },'):
            self.assertIn(split_step, configuration)
        self.assertTrue(all(
            "gate" not in ability
            for ability in parsed_configuration["abilities"].values()))
        self.assertEqual(
            parsed_configuration["workflow"]["plan"],
            [{"action": "focused_sweep"},
             {"role": "plan_reviewer_fixer", "adapter": "codex"},
             {"action": "focused_sweep"},
             {"role": "plan_reviewer_fixer", "adapter": "codex"},
             {"action": "focused_sweep"}])
        self.assertEqual(parsed_configuration["workflow"]["integration"][0],
                         {"action": "full_verify"})
        self.assertEqual(parsed_configuration["workflow"]["integration"][-1],
                         {"action": "full_verify"})
        self.assertEqual(
            parsed_configuration["workflow"]["integration"][1],
            {"role": "reviewer_fixer", "adapter": "codex"})
        prompts = re.findall(r'^prompt = "([^"]+)"$', configuration, re.MULTILINE)
        self.assertEqual(len(prompts), 8)
        self.assertIn("current task", parsed_configuration["abilities"]
                      ["task_review"]["prompt"])
        self.assertIn("cumulative worktree", parsed_configuration["abilities"]
                      ["plan_review"]["prompt"])

        instructions = contracts.installed_contract_text("instructions.md")
        self.assertNotIn("[auto_fix.review]", instructions)
        self.assertNotIn("version-6 record", instructions)
        self.assertNotIn("review_round_index", instructions)
        self.assertIn("[workflow].plan", instructions)
        self.assertIn("workflow_step_index", instructions)
        reading_guide = instructions.split(
            "A **meeting / interactive session** reads only", 1)[1].split(
                "An **assent-scheduled task session** reads only:", 1)[0]
        for key in ("`[workflow]`", "`[roles]`", "`[abilities]`"):
            self.assertIn(key, reading_guide)
        self.assertIn("canonical owner", reading_guide)

    def test_quota_examples_describe_rotation_action(self):
        install_global_contracts(self)
        format_text = contracts.installed_contract_text("format.md")
        self.assertIn(
            'summary = "quota exhausted; progress kept, switching to codex '
            'immediately"', format_text)
        self.assertNotIn("waiting for reset to continue", format_text)

        english = (_PROJECT_ROOT / "docs/CONFIGURATION.md").read_text(
            encoding="utf-8")
        for phrase in (
                "Quota exhaustion records a WIP checkpoint",
                "rotates immediately to the next configured adapter",
                "waits only after all are exhausted"):
            with self.subTest(document="docs/CONFIGURATION.md", phrase=phrase):
                self.assertIn(phrase, english)
        chinese = (_PROJECT_ROOT / "docs/zh-TW/CONFIGURATION.md").read_text(
            encoding="utf-8")
        for phrase in ("quota 中斷會記錄 WIP", "adapter list 會立即切到下一個",
                       "全部用盡才等待"):
            with self.subTest(document="docs/zh-TW/CONFIGURATION.md",
                              phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_checkpoint_resume_control_is_documented_consistently(self):
        install_global_contracts(self)
        record = '{"type":"assent.checkpoint_resume"}'
        format_text = (contracts.installed_contract_text("format.md") + "\n"
                       + contracts.installed_contract_text("workflow.md"))
        english = (_PROJECT_ROOT / "docs/CONFIGURATION.md").read_text(
            encoding="utf-8")
        for name, text in (("format.md+workflow.md", format_text),
                           ("docs/CONFIGURATION.md", english)):
            with self.subTest(document=name):
                self.assertIn(record, text)
                self.assertIn("checkpoint_resume", text)
                compact = " ".join(text.split())
                self.assertIn(
                    "A wrapper may replace a provider quota result with it only "
                    "after arranging an immediate continuation; if it forwards "
                    "provider quota, Assent performs the normal wait or rotation. "
                    "When quota evidence and this record are both present, the "
                    "ordinary quota path wins.",
                    compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/CONFIGURATION.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        self.assertIn(record, chinese)
        self.assertIn("checkpoint_resume", chinese)
        for phrase in (
                "先安排立即續跑，才可把providerquotaresult換成這個record",
                "若轉送providerquota，Assent仍負責普通wait或rotation",
                "若quotaevidence與這個record同時存在，普通quotapath優先"):
            with self.subTest(document="docs/zh-TW/CONFIGURATION.md",
                              phrase=phrase):
                self.assertIn(phrase, compact)

    def test_wip_progress_and_terminal_auto_boundary_are_documented(self):
        install_global_contracts(self)
        format_text = (contracts.installed_contract_text("format.md") + "\n"
                       + contracts.installed_contract_text("workflow.md"))
        english = (_PROJECT_ROOT / "docs/WORKFLOW.md").read_text(
            encoding="utf-8")
        for name, text in (("format.md+workflow.md", format_text),
                           ("docs/WORKFLOW.md", english)):
            compact = " ".join(text.split())
            for phrase in (
                    "progress-bearing WIP checkpoint",
                    "terminal auto",
                    "clean legacy `DONE` task",
                    "does not retroactively synthesize"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/WORKFLOW.md").read_text(
            encoding="utf-8")
        for phrase in ("具備進度的 `WIP`", "終端 auto checkpoint",
                       "乾淨 legacy `DONE` task", "不會改寫或捏造歷史證據"):
            with self.subTest(document="docs/zh-TW/WORKFLOW.md", phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_command_guides_state_the_per_subcommand_config_contract(self):
        commands = (
            "run", "status", "check", "report", "verify", "clean", "archive",
            "accept", "reconcile", "reject", "rework",
        )
        english = (_PROJECT_ROOT / "docs/COMMANDS.md").read_text(
            encoding="utf-8")
        chinese = (_PROJECT_ROOT / "docs/zh-TW/COMMANDS.md").read_text(
            encoding="utf-8")
        for name, text in (("docs/COMMANDS.md", english),
                           ("docs/zh-TW/COMMANDS.md", chinese)):
            compact = " ".join(text.split())
            with self.subTest(document=name):
                clause = (
                    "`run`, `status`, `check`, `report`, `verify`, `clean`, "
                    "`archive`, `accept`, `reconcile`, `reject`, and `rework` "
                    "accept `--config PATH`"
                    if name == "docs/COMMANDS.md" else
                    "`run`、`status`、`check`、`report`、`verify`、`clean`、"
                    "`archive`、`accept`、`reconcile`、`reject`、`rework` "
                    "支援 `--config PATH`")
                self.assertIn(
                    clause if name == "docs/COMMANDS.md"
                    else "".join(clause.split()),
                    compact if name == "docs/COMMANDS.md"
                    else "".join(compact.split()))
                for command in commands:
                    self.assertIn(f"`{command}`", compact)
                self.assertIn(
                    "per-subcommand" if name == "docs/COMMANDS.md"
                    else "每個 subcommand 自己的 option", compact)
                self.assertIn("top-level global option", compact)
                self.assertIn(
                    "have their own project-location contracts"
                    if name == "docs/COMMANDS.md"
                    else "各有自己的 project-location contract", compact)

    def test_operations_guides_separate_recovery_and_persistent_evidence(self):
        english = (_PROJECT_ROOT / "docs/OPERATIONS.md").read_text(
            encoding="utf-8")
        chinese = (_PROJECT_ROOT / "docs/zh-TW/OPERATIONS.md").read_text(
            encoding="utf-8")
        english_compact = " ".join(english.split())
        for phrase in (
                "every uncommitted change is provably attributable",
                "marks that task `WIP`",
                "gathers the edits into a `WIP` checkpoint",
                "without opening an AI session",
                "keeps the dirty worktree for human inspection",
                "journal carries structured events plus bounded summaries and adapter classifications",
                "not the full raw adapter stream",
                "rendered terminal session output",
                "without a parent scheduler prefix"):
            with self.subTest(document="docs/OPERATIONS.md", phrase=phrase):
                self.assertIn(phrase, english_compact)
        chinese_compact = " ".join(chinese.split())
        for phrase in (
                "每個未提交變更都能證明屬於要恢復的 task",
                "將 task 標成 `WIP`",
                "收進 `WIP` checkpoint",
                "不開 AI session",
                "保留 dirty worktree 供人類檢查",
                "journal 保存 structured events",
                "不保存完整 raw adapter stream",
                "rendered terminal session output",
                "沒有 parent scheduler prefix"):
            with self.subTest(document="docs/zh-TW/OPERATIONS.md", phrase=phrase):
                self.assertIn(phrase, chinese_compact)
        self.assertNotIn("journal preserves the adapter result, raw output",
                         english_compact)
        self.assertNotIn("journal 會保留 adapter result、raw output",
                         chinese_compact)

    def test_folder_verification_report_refresh_is_documented(self):
        install_global_contracts(self)
        english = {
            "format.md+workflow.md": (
                contracts.installed_contract_text("format.md") + "\n"
                + contracts.installed_contract_text("workflow.md")),
            "docs/VERIFICATION.md": (_PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                encoding="utf-8"),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (
                ("refreshes `_report.md` after", "best-effort")
                if name == "format.md+workflow.md" else
                ("refreshes that folder's `_report.md` exactly once",
                 "all verification locks are released",
                 "best-effort report refresh"))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        for phrase in ("恰好刷新一次該folder的`_report.md`",
                       "receiptoperationsettle且所有verificationlock釋放後",
                       "best-effortreportrefresh"):
            with self.subTest(document="docs/zh-TW/VERIFICATION.md",
                              phrase=phrase):
                self.assertIn(phrase, compact)

    def test_format_states_the_provisioned_candidate_link_rule(self):
        install_global_contracts(self)
        text = contracts.installed_contract_text("workflow.md")
        for phrase in (
                "reviewed-profile ignored\ndirectory links Assent provisioned",
                "ordinary ignored leaf files that sit inside an otherwise "
                "tracked\ndirectory",
                "Arbitrary ignored content is never exposed",
                "removed before the temporary worktree"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_the_task_session_is_told_to_review_a_required_ignored_directory(self):
        """A zero-memory session reads instructions.md, never format.md."""
        install_global_contracts(self)
        text = " ".join(
            contracts.installed_contract_text("instructions.md").split())
        for phrase in (
                "assent shared-paths review --path DIR --watch FILE",
                "Never hand-create a source-worktree link",
                "Never copy the ignored directory tree in",
                "never modify anything inside the linked target"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        # The scheduled reading scope must stay as it is: the session is not
        # sent to the format contract to learn this.
        scope = text.split("An **assent-scheduled task session** reads only:")[1]
        self.assertNotIn("format.md", scope.split("## Working rules")[0])

    def test_contract_ownership_and_filename_language_have_one_owner(self):
        install_global_contracts(self)
        instructions = " ".join(
            contracts.installed_contract_text("instructions.md").split())
        format_text = " ".join(
            contracts.installed_contract_text("format.md").split())
        for phrase in (
                "repository-specific development constraints belong in `AGENTS.md`",
                "scheduled-session procedure belongs in `instructions.md`",
                "persisted artifact schemas, filename rules, and state meanings",
                "CLI, report, and receipt contracts belong in `workflow.md`",
                "Other documents may reference an owned rule, but must not duplicate it"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, instructions)
        for phrase in (
                "descriptive `name` segment has no canonical-language requirement",
                "preserves the human-requested language, including Unicode",
                "task identity and dependency references use only the filename prefix `tNNN`",
                "paired `.r.toml` journal keeps the same descriptive segment"):
            with self.subTest(document="format.md", phrase=phrase):
                self.assertIn(phrase, format_text)
        self.assertNotIn("no canonical-language requirement", instructions)
        self.assertNotIn("human-requested language", instructions)

    def test_the_ignored_input_diagnosis_is_documented_in_english_and_chinese(self):
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md+workflow.md": (
                contracts.installed_contract_text("format.md") + "\n"
                + contracts.installed_contract_text("workflow.md")),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (("`Ignored input diagnosis:`", "junction")
                       if name != "docs/VERIFICATION.md" else
                       ("Ignored input diagnosis:", "junction",
                        "shared-paths review"))
            with self.subTest(document=name):
                for phrase in phrases:
                    with self.subTest(phrase=phrase):
                        self.assertIn(phrase, compact)
        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            phrases = (("`Ignoredinputdiagnosis:`", "junction")
                       if name != "docs/zh-TW/VERIFICATION.md" else
                       ("Ignoredinputdiagnosis:", "junction",
                        "assentshared-pathsreview"))
            with self.subTest(document=name):
                for phrase in phrases:
                    with self.subTest(phrase=phrase):
                        self.assertIn(phrase, compact)

    def test_shared_path_states_are_documented_in_english_and_chinese(self):
        """The three-state contract and its staleness rules reach every reader."""
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md+workflow.md": (
                contracts.installed_contract_text("format.md") + "\n"
                + contracts.installed_contract_text("workflow.md")),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        session = english.pop("instructions.md")
        for name, text in english.items():
            compact = " ".join(text.split())
            for phrase in ("REVIEWED-NONE", "REVIEWED-PATHS", "STALE",
                           "`.assent/manifest.toml`",
                           "assent shared-paths review"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
        # The scheduled session reads only instructions.md, so the remedy -- not
        # the state vocabulary a scheduler owns -- is what has to be there.
        compact = " ".join(session.split())
        for phrase in ("`.assent/manifest.toml`", "`UNKNOWN` or `STALE`",
                       "assent shared-paths review --path DIR --watch FILE",
                       "assent shared-paths review --none --watch FILE"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, compact)

        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            for phrase in ("REVIEWED-NONE", "REVIEWED-PATHS", "STALE",
                           "`.assent/manifest.toml`",
                           "assentshared-pathsreview",
                           "`shared_inputs_sha256`"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

    def test_the_zero_candidate_fast_path_is_documented_everywhere(self):
        """The state's exact meaning and its limits must reach every reader.

        Naming it is not enough: the documents have to say that it describes a
        successful ignored-entry query and not a semantic "nothing is needed",
        which is precisely the claim a reader would otherwise assume.
        """
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md+workflow.md": (
                contracts.installed_contract_text("format.md") + "\n"
                + contracts.installed_contract_text("workflow.md")),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        session = english.pop("instructions.md")
        for name, text in english.items():
            compact = " ".join(text.split())
            phrases = (("NO-IGNORED-DIRECTORY-CANDIDATE",
                        "ignored-entry query",
                        "semantically needs no shared input")
                       if name != "docs/VERIFICATION.md" else
                       ("NO-IGNORED-DIRECTORY-CANDIDATE",
                        "ignored-entry query",
                        "not a semantic claim that the project never needs "
                        "shared input"))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
        compact = " ".join(session.split())
        for phrase in ("NO-IGNORED-DIRECTORY-CANDIDATE",
                       "nothing to review"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, compact)

        chinese = {
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
            "docs/zh-TW/VERIFICATION.md": (
                _PROJECT_ROOT / "docs/zh-TW/VERIFICATION.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            for phrase in ("NO-IGNORED-DIRECTORY-CANDIDATE",
                           "ignored-entry", "語意上"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

    def test_link_cleanup_contract_is_present_in_all_reader_documents(self):
        documents = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md+workflow.md": (
                contracts.installed_contract_text("format.md") + "\n"
                + contracts.installed_contract_text("workflow.md")),
            "docs/OPERATIONS.md": (_PROJECT_ROOT / "docs/OPERATIONS.md").read_text(
                encoding="utf-8"),
        }
        required = (
            "Assent detaches each directory-link object before any recursive "
            "Git or filesystem removal and never traverses its resolved target.",
            "External link targets survive success, refusal, failure, "
            "interruption, and retry.",
        )
        for name, text in documents.items():
            compact = " ".join(text.split())
            phrases = (required if name != "docs/OPERATIONS.md" else (
                "A directory junction, directory symlink, or other directory "
                "reparse point is detached as a link object before any recursive "
                "Git or filesystem removal.",
                "The remover never traverses the link's resolved target.",
                "External targets survive success, refusal, failure, "
                "interruption, and retry."))
            for phrase in phrases:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "docs/zh-TW/OPERATIONS.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        for phrase in (
                "directoryjunction、directorysymlink或其他directoryreparsepoint會先以"
                "linkobject脫離",
                "絕不穿越resolvedtarget",
                "外部target在成功、拒絕、失敗、中斷與重試後都保留"):
            with self.subTest(document="docs/zh-TW/OPERATIONS.md",
                              phrase=phrase):
                self.assertIn(phrase, compact)

    def test_orphaned_temporary_branch_sweep_contract_is_documented(self):
        """instructions.md and workflow.md both state the lock-based orphan proof."""
        instructions = contracts.installed_contract_text("instructions.md")
        workflow = contracts.installed_contract_text("workflow.md")

        for phrase in (
                "assent-integration/<folder>/<suffix>",
                "assent-reconcile/<folder>",
                "orphaned only when",
        ):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, instructions)

        for phrase in (
                "assent-integration/<folder>/<suffix>",
                "assent-reconcile/<folder>",
                "proven orphaned by the repository-wide integration lock",
                "published",
                "superseded",
                "reporting information only",
                "never the deletion criterion",
        ):
            with self.subTest(document="workflow.md", phrase=phrase):
                self.assertIn(phrase, workflow)

    def test_limited_run_defers_automatic_integration(self):
        """`--once` and `--task` do not integrate an incomplete folder."""
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md+workflow.md": (
                contracts.installed_contract_text("format.md") + "\n"
                + contracts.installed_contract_text("workflow.md")),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            with self.subTest(document=name):
                self.assertIn("defer", compact.lower())
                self.assertIn("incomplete", compact.lower())
                self.assertNotIn("不能與刻意在資料夾收尾前停止的", compact)

    def test_workflow_repair_lifecycle_and_derived_state_are_documented(self):
        """The automatic bounded workflow remains discoverable in owning surfaces."""
        install_global_contracts(self)
        root = _PROJECT_ROOT
        english = {
            "AGENTS.md": (root / "AGENTS.md").read_text(encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text("instructions.md"),
            "README.md": (root / "README.md").read_text(encoding="utf-8"),
            "WORKFLOW.md": (root / "docs/WORKFLOW.md").read_text(encoding="utf-8"),
            "COMMANDS.md": (root / "docs/COMMANDS.md").read_text(encoding="utf-8"),
            "CONFIGURATION.md": (root / "docs/CONFIGURATION.md").read_text(
                encoding="utf-8"),
            "VERIFICATION.md": (root / "docs/VERIFICATION.md").read_text(
                encoding="utf-8"),
            "OPERATIONS.md": (root / "docs/OPERATIONS.md").read_text(
                encoding="utf-8"),
            "CONSENSUS.md": (root / "docs/CONSENSUS.md").read_text(
                encoding="utf-8"),
        }
        required = ("focused_test", "focused_sweep", "full_verify",
                    "finite", "never creates tasks", "never accepts",
                    "_auto_fix.toml")
        for name, text in english.items():
            compact = " ".join(text.split())
            with self.subTest(document=name):
                self.assertNotIn("--auto-fix", compact)
                self.assertNotIn("receipt_refresh", compact)
        english_contract = " ".join(" ".join(text.split())
                                    for text in english.values())
        for phrase in required:
            with self.subTest(english_contract=phrase):
                self.assertIn(phrase, english_contract)

        format_text = english["format.md"] + "\n" + contracts.installed_contract_text(
            "workflow.md")
        for field in (
                "source_tree", "task_plan_sha256", "review_prompt_sha256",
                "reviewer_role", "reviewer_adapter", "reviewer_model", "reviewer_effort",
                "current_finding_fingerprints", "observed_states",
                "workflow_step_index", "reviewer_step_index"):
            with self.subTest(state_field=field):
                self.assertIn(field, format_text)
        for phrase in (
                "malformed state refuses closed",
                "PASSED (fresh)", "FAILED (fresh)",
                "Automatic repair of durable folder-review findings",
                "authorization: configured workflow repair",
                "prompt-plus-detection",
                "source deletion",
                "focused sweep"):
            with self.subTest(format_phrase=phrase):
                self.assertIn(phrase, format_text)

        configuration = (root / "assent/templates/assent.toml").read_text(
            encoding="utf-8")
        self.assertIn("[workflow]", configuration)
        self.assertIn('role = "reviewer_fixer"', configuration)
        self.assertNotIn("folder_reviewer", configuration)
        self.assertIn("Plan roles handle only focused_sweep", configuration)
        self.assertIn("plan = [", configuration)

        chinese = {
            "WORKFLOW.zh-TW.md": (root / "docs/zh-TW/WORKFLOW.md").read_text(
                encoding="utf-8"),
            "COMMANDS.zh-TW.md": (root / "docs/zh-TW/COMMANDS.md").read_text(
                encoding="utf-8"),
            "CONFIGURATION.zh-TW.md": (
                root / "docs/zh-TW/CONFIGURATION.md").read_text(encoding="utf-8"),
            "VERIFICATION.zh-TW.md": (
                root / "docs/zh-TW/VERIFICATION.md").read_text(encoding="utf-8"),
            "OPERATIONS.zh-TW.md": (
                root / "docs/zh-TW/OPERATIONS.md").read_text(encoding="utf-8"),
            "CONSENSUS.zh-TW.md": (
                root / "docs/zh-TW/CONSENSUS.md").read_text(encoding="utf-8"),
        }
        chinese_contract = "".join("".join(text.split())
                                   for text in chinese.values())
        self.assertNotIn("--auto-fix", chinese_contract)
        self.assertNotIn("receipt_refresh", chinese_contract)
        self.assertIn("_auto_fix.toml", chinese_contract)

    def test_selection_conflict_repair_is_owned_by_packaged_contracts(self):
        install_global_contracts(self)
        format_text = contracts.installed_contract_text("format.md")
        workflow_text = " ".join(
            contracts.installed_contract_text("workflow.md").split())
        instructions_text = " ".join(
            contracts.installed_contract_text("instructions.md").split())
        configuration = (_PROJECT_ROOT / "assent/templates/assent.toml").read_text(
            encoding="utf-8")

        for phrase in (
                "[workflow].task", "[workflow].plan", "[workflow].integration",
                "focused_sweep", "full_verify", "content-identical"):
            with self.subTest(format_phrase=phrase):
                self.assertIn(phrase, format_text)
        for phrase in (
                "typed conflict wave", "target-alone", "peer-only",
                "base/ours/theirs", "zero full-test runs", "never accepts a prefix"):
            with self.subTest(workflow_phrase=phrase):
                self.assertIn(phrase, workflow_text)
        for phrase in (
                "configured integration workflow", "Neither form may run Git", "full suite",
                "review-and-repair action"):
            with self.subTest(instructions_phrase=phrase):
                self.assertIn(phrase, instructions_text)
        self.assertIn("integration = [", configuration)
        self.assertIn("[roles.fixer]", configuration)
        self.assertIn('ability = ["review", "fix"]', configuration)

    def test_round_interruption_and_gated_settle_are_documented(self):
        """Interrupted-round recovery, the gated settle, the failing-gate
        disposition, and the unresolved-review outcome must all be
        discoverable in the installed contract and both reader-doc surfaces.
        """
        install_global_contracts(self)
        root = _PROJECT_ROOT
        english = {
            "instructions.md": contracts.installed_contract_text("instructions.md"),
            "workflow.md": contracts.installed_contract_text("workflow.md"),
            "WORKFLOW.md": (root / "docs/WORKFLOW.md").read_text(encoding="utf-8"),
            "COMMANDS.md": (root / "docs/COMMANDS.md").read_text(encoding="utf-8"),
            "VERIFICATION.md": (root / "docs/VERIFICATION.md").read_text(
                encoding="utf-8"),
        }
        required = (
            "REVIEW UNRESOLVED, HUMAN DECISION",
            "settling gate",
            "REPAIRING",
            "AWAITING_REVIEW",
            "wip",
            "fail-closed",
            "de-duplicating ledger",
        )
        english_contract = " ".join(" ".join(text.split())
                                    for text in english.values())
        for phrase in required:
            with self.subTest(english_contract=phrase):
                self.assertIn(phrase, english_contract)
        # The failing-gate disposition must read as its own outcome, distinct
        # from SELF-FIXED, UNREVIEWED and from an ordinary BLOCKED task.
        self.assertIn("distinct", english_contract)
        self.assertIn("does not settle", english_contract)
        # The stale claim that exhaustion with open findings ends the run
        # nonzero must no longer appear anywhere in these surfaces.
        self.assertNotIn(
            "an unrepaired blocker preserves every finding, edit, and journal "
            "without another round and exits nonzero", english_contract)
        self.assertNotIn(
            "on an unrepaired blocker preserves every finding, edit, and "
            "journal and exits nonzero", english_contract)

        chinese = {
            "WORKFLOW.zh-TW.md": (root / "docs/zh-TW/WORKFLOW.md").read_text(
                encoding="utf-8"),
            "COMMANDS.zh-TW.md": (root / "docs/zh-TW/COMMANDS.md").read_text(
                encoding="utf-8"),
            "VERIFICATION.zh-TW.md": (
                root / "docs/zh-TW/VERIFICATION.md").read_text(encoding="utf-8"),
        }
        translated_required = (
            "REVIEW UNRESOLVED, HUMAN DECISION",
            "settling gate",
            "de-duplicating ledger",
            "fail-closed",
        )
        chinese_contract = "".join("".join(text.split())
                                   for text in chinese.values())
        for phrase in translated_required:
            with self.subTest(chinese_contract=phrase):
                self.assertIn("".join(phrase.split()), chinese_contract)

    def test_auto_fix_state_schema_matches_the_version_seven_contract(self):
        """The executable state shape and packaged contract must advance together."""
        install_global_contracts(self)
        from dataclasses import fields

        from assent import auto_fix

        format_text = contracts.installed_contract_text("workflow.md")
        self.assertEqual(auto_fix.AUTO_FIX_STATE_VERSION, 7)
        self.assertEqual(
            {field.name for field in fields(auto_fix.AutoFixState)},
            auto_fix._STATE_KEYS)
        for phrase in (
                "Version 7 has exactly these scalar fields",
                "version = 7",
                "phase = \"COMPLETE\"",
                "NEEDS_REPAIR", "REPAIRING", "AWAITING_REVIEW", "COMPLETE",
                "A restart resumes `REPAIRING` or `AWAITING_REVIEW`",
                "missing or drifted workflow configuration",
                "refuses repair and closeout",
        ):
            with self.subTest(format_phrase=phrase):
                self.assertIn(phrase, format_text)
        self.assertNotIn("Version 1 has", format_text)
        for field in (
                "review_context", "review_stage", "failure_trigger",
                "reviewer_recommendations", "approved_scope_additions",
                "scope_amendments", "worker_dispositions", "repair_briefs",
                "workflow_step_index",
                "reviewer_step_index", "reviewer_role", "self_fixed_unreviewed",
                "plan_digest_transitions", "review_transitions"):
            with self.subTest(state_field=field):
                self.assertIn(field, format_text)
        self.assertIn("ASSENT_REPAIR_DISPOSITION", format_text)

    def test_version_seven_example_is_parseable_and_finding_identity_is_complete(self):
        """The packaged state example must be usable as TOML and describe its full identity."""
        format_text = contracts.installed_contract_text("format.md")
        workflow_text = contracts.installed_contract_text("workflow.md")
        match = re.search(
            r"```toml\n(version = 7\n.*?)(?:\n```)", workflow_text, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        example = tomllib.loads(match.group(1))
        from assent import auto_fix
        self.assertEqual(
            set(example["findings"][0]), auto_fix._PERSISTED_FINDING_KEYS)
        self.assertIn(
            "`kind`, `task_id`, `path`, `summary`, `evidence`,", workflow_text)
        self.assertIn(
            "`recommendation`, and the optional `scope_addition` path", workflow_text)
        self.assertIn("one additional, separately reviewed exception", format_text)
        self.assertIn("append the exact approved paths", format_text)

    def test_reader_recovery_never_recommends_raw_recursive_worktree_removal(self):
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
                self.assertNotIn("請自行執行\n`git", text)


class TestContractValidation(unittest.TestCase):
    def test_current_contracts_report_no_error(self):
        install_global_contracts(self)
        self.assertEqual(contracts.contract_errors(), [])
        contracts.require_contracts()  # must not raise

    def test_a_windows_editor_rewriting_line_endings_stays_current(self):
        home = install_global_contracts(self)
        path = home / "instructions.md"
        path.write_bytes(
            path.read_text(encoding="utf-8").replace("\n", "\r\n")
            .encode("utf-8"))
        self.assertEqual(contracts.contract_errors(), [])

    def test_missing_contracts_are_named_one_by_one(self):
        home = install_global_contracts(self)
        (home / "format.md").unlink()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn(str(home / "format.md"), errors[0])
        self.assertIn("missing", errors[0])

    def test_an_empty_user_home_names_both_contracts(self):
        home = install_global_contracts(self)
        for name in contracts.CONTRACT_NAMES:
            (home / name).unlink()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), len(contracts.CONTRACT_NAMES))
        for name, error in zip(contracts.CONTRACT_NAMES, errors):
            with self.subTest(contract=name):
                self.assertIn(str(home / name), error)

    def test_a_stale_contract_is_reported_as_differing(self):
        home = install_global_contracts(self)
        (home / "instructions.md").write_text(
            "an older assent's working instructions\n", encoding="utf-8")
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("stale", errors[0])
        self.assertIn("instructions.md", errors[0])

    def test_an_unreadable_contract_is_reported_not_raised(self):
        home = install_global_contracts(self)
        (home / "format.md").unlink()
        (home / "format.md").mkdir()
        errors = contracts.contract_errors()
        self.assertEqual(len(errors), 1)
        self.assertIn("cannot be read", errors[0])

    def test_require_contracts_fails_closed_with_the_init_remedy(self):
        home = install_global_contracts(self)
        (home / "instructions.md").unlink()
        with self.assertRaises(AssentError) as ctx:
            contracts.require_contracts()
        message = str(ctx.exception)
        self.assertIn(str(home / "instructions.md"), message)
        self.assertIn("assent init", message)


if __name__ == "__main__":
    unittest.main()
