"""agents.toml 載入與驗證測試。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agents import AgentsError
from agents.config import list_task_folders, load_config

_MINIMAL = ""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.agents_dir = self.root / ".agents"
        self.agents_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, text: str) -> Path:
        path = self.agents_dir / "agents.toml"
        path.write_text(text, encoding="utf-8")
        return path


class TestLoadConfig(ConfigTestCase):
    def test_minimal_config_and_defaults(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(cfg.root, self.root.resolve())
        self.assertEqual(cfg.agents_dir, self.agents_dir.resolve())
        self.assertEqual(cfg.tasks_name, "plan01")
        self.assertEqual(cfg.tasks_dir, self.agents_dir.resolve() / "plan01")
        self.assertEqual(cfg.branch_prefix, "plan01/")
        self.assertEqual(cfg.stall_minutes, 30)
        self.assertEqual(cfg.retry_per_task, 1)
        self.assertEqual(cfg.quota_poll_minutes, 30)
        self.assertEqual(cfg.adapter_name, "claude")
        self.assertEqual(cfg.claude_models["prime"], "fable")
        self.assertEqual(cfg.codex_models["lite"], "gpt-5.6-luna")
        self.assertIsNone(cfg.prompt_template)

    def test_runtime_artifact_paths(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(cfg.runtime_log_rel, ".agents/plan01/_agents.log")
        self.assertEqual(cfg.report_rel, ".agents/plan01/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".agents/plan01/agents.lock")
        self.assertEqual(cfg.git_excludes,
                         (".agents/plan01/_agents.log", ".agents/plan01/_report.md",
                         ".agents/plan01/agents.lock"))

    def test_provided_folder_updates_all_derived_paths(self):
        cfg = load_config(self.write(_MINIMAL), folder="parallel02")
        self.assertEqual(cfg.tasks_name, "parallel02")
        self.assertEqual(cfg.tasks_dir, self.agents_dir.resolve() / "parallel02")
        self.assertEqual(cfg.branch_prefix, "parallel02/")
        self.assertEqual(cfg.runtime_log_rel, ".agents/parallel02/_agents.log")
        self.assertEqual(cfg.report_rel, ".agents/parallel02/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".agents/parallel02/agents.lock")

    def test_missing_file_raises(self):
        with self.assertRaises(AgentsError):
            load_config(self.agents_dir / "agents.toml", "plan01")

    def test_removed_plan_section_rejected_as_unknown_key(self):
        with self.assertRaisesRegex(AgentsError, "未知的頂層鍵"):
            load_config(self.write('[plan]\ntasks = "plan01"\n'), "plan01")

    def test_invalid_toml_raises(self):
        with self.assertRaises(AgentsError):
            load_config(self.write("[run\nretry_per_task ="), "plan01")

    def test_unknown_top_level_key_raises(self):
        with self.assertRaisesRegex(AgentsError, "未知的頂層鍵"):
            load_config(self.write("[plann]\nx = 1\n"), "plan01")

    def test_removed_git_section_rejected_as_unknown_key(self):
        with self.assertRaisesRegex(AgentsError, "未知的頂層鍵"):
            load_config(self.write("[git]\nenabled = false\n"), "plan01")

    def test_folder_name_with_space_rejected(self):
        with self.assertRaisesRegex(AgentsError, "工作資料夾名稱"):
            load_config(self.write(_MINIMAL), "my plan")

    def test_folder_name_with_slash_rejected(self):
        for bad in ("a/b", "a\\\\b"):
            with self.assertRaises(AgentsError):
                load_config(self.write(_MINIMAL), bad)

    def test_folder_name_leading_dash_or_dot_rejected(self):
        for bad in ("-x", ".x"):
            with self.assertRaises(AgentsError):
                load_config(self.write(_MINIMAL), bad)

    def test_invalid_folder_override_rejected(self):
        with self.assertRaisesRegex(AgentsError, "命令列工作資料夾"):
            load_config(self.write(_MINIMAL), folder="bad/name")

    def test_type_error_reported(self):
        with self.assertRaisesRegex(AgentsError, "型別錯誤"):
            load_config(self.write("[watchdog]\nstall_minutes = \"x\"\n"), "plan01")

    def test_negative_stall_rejected(self):
        with self.assertRaises(AgentsError):
            load_config(self.write("[watchdog]\nstall_minutes = -1\n"), "plan01")

    def test_models_table_full_replacement(self):
        cfg = load_config(self.write(
            '[adapter.claude.models]\nprime = "x"\n'), "plan01")
        self.assertEqual(cfg.claude_models, {"prime": "x"})  # 整表取代,不合併

    def test_bad_default_effort_rejected(self):
        with self.assertRaisesRegex(AgentsError, "effort"):
            load_config(self.write(
                '[adapter.claude.default_effort]\nprime = "max"\n'), "plan01")

    def test_prompt_template_loaded(self):
        cfg = load_config(self.write(
            '[prompt]\ntemplate = "hi {task_id}"\n'), "plan01")
        self.assertEqual(cfg.prompt_template, "hi {task_id}")


class TestListTaskFolders(ConfigTestCase):
    def test_lists_only_visible_folders_containing_formal_task_files(self):
        for name, filename in (("beta", "t002_b.e.toml"),
                               ("alpha", "t001_a.e.toml"),
                               ("empty", "notes.txt"),
                               ("_hidden", "t001_h.e.toml"),
                               ("__pycache__", "t001_c.e.toml")):
            folder = self.agents_dir / name
            folder.mkdir()
            (folder / filename).write_text("", encoding="utf-8")
        self.assertEqual(list_task_folders(self.agents_dir), ["alpha", "beta"])

    def test_missing_agents_directory_is_empty(self):
        self.assertEqual(list_task_folders(self.root / "missing"), [])


if __name__ == "__main__":
    unittest.main()
