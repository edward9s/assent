"""CLI 進入點與 init 測試。run 會 tee 到工作資料夾的 _agents.log,
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
                    ".agents/instructions.md", ".agents/verify.py",
                    "AGENTS.md", ".gitignore"):
            self.assertTrue((self.root / rel).is_file(), rel)
        self.assertTrue((self.root / ".agents" / "plan01").is_dir())
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        lines = gitignore.splitlines()
        self.assertIn(".agents/", lines)
        self.assertNotIn("AGENTS.md", lines)
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md.count("<!-- agents-instructions -->"), 1)
        self.assertNotIn("## AI 工作體系", agents_md)

    def test_idempotent_no_overwrite_no_duplicates(self):
        run_init(self.root)
        (self.root / ".agents" / "agents.toml").write_text(
            '[plan]\ntasks = "custom"\n', encoding="utf-8")
        (self.root / ".agents" / "instructions.md").write_text(
            "本機自訂指示\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root), 0)
        # 不覆蓋既有設定
        self.assertIn("custom", (self.root / ".agents" / "agents.toml")
                      .read_text(encoding="utf-8"))
        # gitignore 不重複累加
        gitignore = (self.root / ".gitignore").read_text(encoding="utf-8")
        self.assertEqual(gitignore.splitlines().count(".agents/"), 1)
        # 已有檔案不覆蓋,AGENTS.md 橋接不重複
        self.assertEqual((self.root / ".agents" / "instructions.md")
                         .read_text(encoding="utf-8"), "本機自訂指示\n")
        agents_md = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertEqual(agents_md.count("<!-- agents-instructions -->"), 1)

    def test_adds_one_bridge_line_to_existing_agents_md(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n既有規則。\n", encoding="utf-8")
        run_init(self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 我的專案"))
        self.assertIn("既有規則。", text)
        self.assertNotIn("## AI 工作體系", text)
        self.assertEqual(text.count("<!-- agents-instructions -->"), 1)

    def test_migrates_legacy_section_without_touching_later_project_section(self):
        (self.root / "AGENTS.md").write_text(
            "# 我的專案\n\n既有規則。\n\n"
            "## AI 工作體系(.agents)\n\n舊 agents 內容。\n\n"
            "## 專案附註\n\n必須保留。\n", encoding="utf-8")
        run_init(self.root)
        text = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertNotIn("舊 agents 內容", text)
        self.assertIn("## 專案附註\n\n必須保留。", text)
        self.assertEqual(text.count("<!-- agents-instructions -->"), 1)

    def test_preserves_agents_md_ignore_choice(self):
        (self.root / ".gitignore").write_text(
            "cache/\nAGENTS.md\n.agents/\n", encoding="utf-8")
        run_init(self.root)
        lines = (self.root / ".gitignore").read_text(
            encoding="utf-8").splitlines()
        self.assertIn("cache/", lines)
        self.assertIn(".agents/", lines)
        self.assertIn("AGENTS.md", lines)

    def test_missing_target_dir_fails(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(self.root / "nope"), 1)


if __name__ == "__main__":
    unittest.main()
