"""contracts tests: the global ~/.assent/instructions.md and format.md gate.

Every case redirects the user home with ASSENT_HOME, so the developer's real
~/.assent is never read or written.  ``install_global_contracts`` is the fixture
the CLI, engine and inspection test modules share to put a temporary user home
with current contracts in place.
"""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError, contracts
from assent.user_home import ASSENT_HOME_ENV

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


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

    def test_format_states_the_provisioned_candidate_link_rule(self):
        install_global_contracts(self)
        text = contracts.installed_contract_text("format.md")
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

    def test_the_ignored_input_diagnosis_is_documented_in_english_and_chinese(self):
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "README.md": (_PROJECT_ROOT / "README.md").read_text(
                encoding="utf-8"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            with self.subTest(document=name):
                self.assertIn("`Ignored input diagnosis:`", compact)
                self.assertIn("junction", compact)
        chinese = {
            "README.zh-TW.md": (_PROJECT_ROOT / "README.zh-TW.md").read_text(
                encoding="utf-8"),
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            with self.subTest(document=name):
                self.assertIn("`Ignoredinputdiagnosis:`", compact)
                self.assertIn("junction", compact)

    def test_shared_path_states_are_documented_in_english_and_chinese(self):
        """The three-state contract and its staleness rules reach every reader."""
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "README.md": (_PROJECT_ROOT / "README.md").read_text(
                encoding="utf-8"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
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
            "README.zh-TW.md": (_PROJECT_ROOT / "README.zh-TW.md").read_text(
                encoding="utf-8"),
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
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
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "README.md": (_PROJECT_ROOT / "README.md").read_text(
                encoding="utf-8"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
        }
        session = english.pop("instructions.md")
        for name, text in english.items():
            compact = " ".join(text.split())
            for phrase in ("NO-IGNORED-DIRECTORY-CANDIDATE",
                           "ignored-entry query",
                           "semantically needs no shared input"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
        compact = " ".join(session.split())
        for phrase in ("NO-IGNORED-DIRECTORY-CANDIDATE",
                       "nothing to review"):
            with self.subTest(document="instructions.md", phrase=phrase):
                self.assertIn(phrase, compact)

        chinese = {
            "README.zh-TW.md": (_PROJECT_ROOT / "README.zh-TW.md").read_text(
                encoding="utf-8"),
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
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
            "format.md": contracts.installed_contract_text("format.md"),
            "README.md": (_PROJECT_ROOT / "README.md").read_text(
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
            for phrase in required:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = (_PROJECT_ROOT / "README.zh-TW.md").read_text(
            encoding="utf-8")
        compact = "".join(chinese.split())
        for phrase in (
                "連結目標清理警告",
                "Assent在任何遞迴Git或檔案系統移除前,都會先解除每個目錄連結物件,"
                "絕不沿解析後的目標路徑走訪。",
                "外部連結目標在成功、拒絕、失敗、中斷與重試後都存活。"):
            with self.subTest(document="README.zh-TW.md", phrase=phrase):
                self.assertIn(phrase, compact)

    def test_limited_run_verification_is_conditional_in_every_document(self):
        """`--verify` with `--once`/`--task` is gated, never blanket-refused."""
        install_global_contracts(self)
        english = {
            "AGENTS.md": (_PROJECT_ROOT / "AGENTS.md").read_text(
                encoding="utf-8"),
            "format.md": contracts.installed_contract_text("format.md"),
            "instructions.md": contracts.installed_contract_text(
                "instructions.md"),
            "README.md": (_PROJECT_ROOT / "README.md").read_text(
                encoding="utf-8"),
            "docs/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/CONSENSUS.md").read_text(
                    encoding="utf-8"),
        }
        required = (
            "verifies only when that limited run left the single selected "
            "folder complete",
            "an incomplete folder fails the request without writing a receipt",
        )
        # The old blanket refusal must not come back anywhere; `...` staying
        # incompatible with the two selectors is a separate, still-true rule.
        forbidden = (
            "`--verify` cannot be combined with `--once`",
            "`--verify` is refused with `--once`",
            "refuse the flag",
            "incompatible with `--once`",
        )
        for name, text in english.items():
            compact = " ".join(text.split())
            for phrase in required:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
            for phrase in forbidden:
                with self.subTest(document=name, forbidden=phrase):
                    self.assertNotIn(phrase, compact)

        chinese = {
            "README.zh-TW.md": (_PROJECT_ROOT / "README.zh-TW.md").read_text(
                encoding="utf-8"),
            "docs/zh-TW/CONSENSUS.md": (
                _PROJECT_ROOT / "docs/zh-TW/CONSENSUS.md").read_text(
                    encoding="utf-8"),
        }
        for name, text in chinese.items():
            compact = "".join(text.split())
            for phrase in ("只有在該次受限執行讓所選資料夾變成完成時才驗證",
                           "資料夾未完成則此請求失敗且不寫下receipt"):
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)
            with self.subTest(document=name, forbidden="blanket refusal"):
                self.assertNotIn("不可與`--once`、`--task`併用", compact)
                self.assertNotIn("不能與刻意在資料夾收尾前停止的", compact)

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
        self.assertEqual(len(errors), 2)
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
