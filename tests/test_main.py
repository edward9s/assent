"""CLI 進入點與 init 測試。run 會 tee 到工作資料夾的 _agents.log,
故一律 chdir 到臨時目錄執行,避免弄髒測試程序的工作目錄。"""
import contextlib
import io
import os
import shutil
import subprocess
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

    def write_config(self, text="") -> Path:
        config = self.root / ".agents" / "agents.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(text, encoding="utf-8")
        return config

    def write_task(self, folder: str, status: str = "TODO") -> Path:
        path = self.root / ".agents" / folder / "t001_task.e.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'title = "任務"\n'
            'deps = []\n'
            'model = "lite"\n'
            f'status = "{status}"\n'
            'scope = ["agents/"]\n'
            'verify = "python -m unittest"\n'
            'goal = "完成任務"\n'
            'acceptance = "驗證通過"\n',
            encoding="utf-8")
        return path


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
        config = self.write_config()
        agents_dir = config.parent
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
        config = self.write_config()
        code, out = self.run_main(["status", "bad/name", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("命令列工作資料夾", out)

    def test_plan_section_reports_dedicated_removal_error(self):
        config = self.write_config('[plan]\ntasks = "A"\n')
        code, out = self.run_main(["status", "A", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("[plan] 區塊已廢除", out)

    def test_run_without_folder_selects_unique_ongoing_folder(self):
        config = self.write_config()
        self.write_task("active", "TODO")
        self.write_task("archive", "DONE")
        with patch("agents.__main__.engine.run", return_value=0) as mocked:
            code, out = self.run_main(["run", "--config", str(config)])
        self.assertEqual(code, 0)
        self.assertIn("工作資料夾:active(唯一進行中,自動選定)", out)
        self.assertEqual(mocked.call_args.args[0].tasks_name, "active")

    def test_run_without_folder_refuses_zero_or_multiple_ongoing(self):
        for case, statuses in (("zero", [("archive", "DONE")]),
                               ("multiple", [("one", "TODO"),
                                             ("two", "WIP")])):
            with self.subTest(case=case):
                shutil.rmtree(self.root / ".agents", ignore_errors=True)
                config = self.write_config()
                for folder, status in statuses:
                    self.write_task(folder, status)
                with patch("agents.__main__.engine.run") as mocked:
                    code, out = self.run_main(
                        ["run", "--config", str(config)])
                self.assertEqual(code, 1)
                self.assertIn("請明寫工作資料夾參數", out)
                for folder, _ in statuses:
                    self.assertIn(folder, out)
                mocked.assert_not_called()

    def test_run_without_folder_refuses_when_no_task_folder_exists(self):
        config = self.write_config()
        code, out = self.run_main(["run", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertIn("未找到含任務檔", out)

    def test_read_only_commands_without_folder_run_all_folders(self):
        config = self.write_config()
        self.write_task("beta")
        self.write_task("alpha")
        for command in ("status", "check", "report"):
            with self.subTest(command=command), patch(
                    f"agents.__main__.engine.{command}", return_value=0) as mocked:
                code, _ = self.run_main([command, "--config", str(config)])
                self.assertEqual(code, 0)
                self.assertEqual(
                    [call.args[0].tasks_name for call in mocked.call_args_list],
                    ["alpha", "beta"])

    def test_check_without_folder_fails_if_any_folder_fails(self):
        config = self.write_config()
        self.write_task("alpha")
        self.write_task("beta")
        with patch("agents.__main__.engine.check", side_effect=[0, 1]) as mocked:
            code, _ = self.run_main(["check", "--config", str(config)])
        self.assertEqual(code, 1)
        self.assertEqual(mocked.call_count, 2)


class TestInit(MainTestCase):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)

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
        config = (self.root / ".agents" / "agents.toml").read_text(
            encoding="utf-8")
        self.assertNotIn("[git]", config)
        self.assertNotIn("[plan]", config)

    def test_idempotent_no_overwrite_no_duplicates(self):
        run_init(self.root)
        (self.root / ".agents" / "agents.toml").write_text(
            '[run]\nretry_per_task = 7\n# custom\n', encoding="utf-8")
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

    def test_no_git_refuses_without_creating_files(self):
        target = self.root / "not-repo"
        target.mkdir()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(run_init(target), 1)
        self.assertIn("本專案尚未初始化 git,請先執行 git init", out.getvalue())
        self.assertFalse((target / ".agents").exists())


if __name__ == "__main__":
    unittest.main()
