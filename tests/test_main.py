"""CLI 進入點與 init 測試。main() 會 tee 到 .agents/agents.log,
故一律 chdir 到臨時目錄執行,避免弄髒測試程序的工作目錄。"""
import contextlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.__main__ import main
from agents.init import init as run_init


class MainTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._old_cwd)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def run_main(self, argv) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(argv)
        return code, out.getvalue()


class TestDispatch(MainTestCase):
    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            with contextlib.redirect_stdout(io.StringIO()):
                main(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_missing_config_reports_error(self):
        code, out = self.run_main(["status"])
        self.assertEqual(code, 1)
        self.assertIn("設定檔錯誤", out)
        self.assertIn("agents init", out)

    def test_run_missing_config_reports_error(self):
        code, out = self.run_main(
            ["run", "--config", str(self.root / "nope" / "agents.toml")])
        self.assertEqual(code, 1)

    def test_all_plan_commands_accept_folder_override(self):
        agents_dir = self.root / ".agents"
        agents_dir.mkdir()
        config = agents_dir / "agents.toml"
        config.write_text('[plan]\ntasks = "A"\n', encoding="utf-8")
        commands = (("run", "run"), ("status", "status"),
                    ("check", "check"), ("report", "report"))
        for command, engine_name in commands:
            with self.subTest(command=command), patch(
                    f"agents.__main__.engine.{engine_name}", return_value=0) as mocked:
                code, _ = self.run_main([command, "B", "--config", str(config)])
                self.assertEqual(code, 0)
                cfg = mocked.call_args.args[0]
                self.assertEqual(cfg.tasks_name, "B")
                self.assertEqual(cfg.tasks_dir, agents_dir.resolve() / "B")

    def test_invalid_folder_override_reports_error(self):
        agents_dir = self.root / ".agents"
        agents_dir.mkdir()
        config = agents_dir / "agents.toml"
        config.write_text('[plan]\ntasks = "A"\n', encoding="utf-8")
        code, out = self.run_main(["status", "bad/name", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("命令列工作資料夾", out)


class TestInit(MainTestCase):
    def test_creates_skeleton(self):
        code, out = self.run_main(["init"])
        self.assertEqual(code, 0)
        for rel in (".agents/agents.toml", ".agents/format.md",
                    ".agents/verify.py", "AGENTS.md", ".gitignore"):
            self.assertTrue((self.root / rel).is_file(), rel)
        self.assertTrue((self.root / ".agents" / "plan01").is_dir())
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".agents/", gitignore.splitlines())
        self.assertNotIn(".agents/agents.log", gitignore.splitlines())
        self.assertNotIn(".agents/*/report.md", gitignore.splitlines())

    def test_idempotent_no_overwrite_no_duplicates(self):
        run_init(self.root)
        (self.root / ".agents" / "agents.toml").write_text(
            '[plan]\ntasks = "custom"\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root), 0)
        # 不覆蓋既有設定
        self.assertIn("custom", (self.root / ".agents" / "agents.toml")
                      .read_text(encoding="utf-8"))
        # gitignore 不重複累加
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gitignore.splitlines().count(".agents/"), 1)
        # AGENTS.md 已含該節,不重複 append
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md.count("## AI 工作體系"), 1)

    def test_merges_section_into_existing_agents_md(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n既有規則。\n", encoding="utf-8")
        run_init(self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 我的專案"))
        self.assertIn("既有規則。", text)
        self.assertIn("## AI 工作體系", text)
        self.assertEqual(text.count("## AI 工作體系"), 1)

    def test_missing_target_dir_fails(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root / "nope"), 1)


if __name__ == "__main__":
    unittest.main()
