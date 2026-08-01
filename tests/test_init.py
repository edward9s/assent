"""Focused tests for the normal shared-contract refresh performed by init."""
import contextlib
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from assent.init import init as run_init
from tests.test_contracts import install_global_contracts

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestInitContractRefresh(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.user_home = install_global_contracts(self)
        subprocess.run(
            ["git", "init"], cwd=self.root, check=True,
            capture_output=True)

    def test_repeat_init_refreshes_the_packaged_format_through_normal_flow(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root, test="unittest"), 0)

        (self.user_home / "format.md").write_text(
            "an older unsafe cleanup contract\n", encoding="utf-8")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(run_init(self.root), 0)

        expected = (_PROJECT_ROOT / "assent/templates/format.md").read_bytes()
        self.assertEqual((self.user_home / "format.md").read_bytes(), expected)
        self.assertFalse((self.root / ".assent/format.md").exists())
        self.assertIn("Updated:", output.getvalue())

    def test_init_installs_the_ignored_input_provisioning_instruction(self):
        """What a scheduled session reads must forbid copying an ignored tree."""
        (self.user_home / "instructions.md").write_text(
            "an older working instruction\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root, test="unittest"), 0)

        text = " ".join((self.user_home / "instructions.md").read_text(
            encoding="utf-8").split())
        self.assertIn("Never copy the ignored directory tree in", text)
        self.assertIn(
            "assent shared-paths review --path DIR --watch FILE", text)
        self.assertIn("Never hand-create a source-worktree link", text)
        self.assertFalse((self.root / ".assent/instructions.md").exists())


if __name__ == "__main__":
    unittest.main()
