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
                "explicitly provisioned ignored root-level directory links",
                "Arbitrary ignored content is never exposed",
                "removed before the temporary worktree"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


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
