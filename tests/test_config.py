"""agents.toml 載入與驗證測試。"""
import shutil
import tempfile
import unittest
from pathlib import Path

from agents import AgentsError
from agents.config import load_config

_MINIMAL = '[plan]\ntasks = "plan01"\n'


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
        cfg = load_config(self.write(_MINIMAL))
        self.assertEqual(cfg.root, self.root.resolve())
        self.assertEqual(cfg.agents_dir, self.agents_dir.resolve())
        self.assertEqual(cfg.tasks_name, "plan01")
        self.assertEqual(cfg.tasks_dir, self.agents_dir.resolve() / "plan01")
        self.assertEqual(cfg.branch_prefix, "plan01/")
        self.assertTrue(cfg.git_enabled)
        self.assertEqual(cfg.stall_minutes, 30)
        self.assertEqual(cfg.retry_per_task, 1)
        self.assertEqual(cfg.quota_poll_minutes, 30)
        self.assertEqual(cfg.adapter_name, "claude")
        self.assertEqual(cfg.claude_models["prime"], "fable")
        self.assertEqual(cfg.codex_models["lite"], "gpt-5.6-luna")
        self.assertIsNone(cfg.prompt_template)

    def test_runtime_artifact_paths(self):
        cfg = load_config(self.write(_MINIMAL))
        self.assertEqual(cfg.runtime_log_rel, ".agents/plan01/_agents.log")
        self.assertEqual(cfg.report_rel, ".agents/plan01/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".agents/plan01/agents.lock")
        self.assertEqual(cfg.git_excludes,
                         (".agents/plan01/_agents.log", ".agents/plan01/_report.md",
                         ".agents/plan01/agents.lock"))

    def test_folder_override_updates_all_derived_paths(self):
        cfg = load_config(self.write(_MINIMAL), folder="parallel02")
        self.assertEqual(cfg.tasks_name, "parallel02")
        self.assertEqual(cfg.tasks_dir, self.agents_dir.resolve() / "parallel02")
        self.assertEqual(cfg.branch_prefix, "parallel02/")
        self.assertEqual(cfg.runtime_log_rel, ".agents/parallel02/_agents.log")
        self.assertEqual(cfg.report_rel, ".agents/parallel02/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".agents/parallel02/agents.lock")

    def test_missing_file_raises(self):
        with self.assertRaises(AgentsError):
            load_config(self.agents_dir / "agents.toml")

    def test_missing_tasks_raises(self):
        with self.assertRaisesRegex(AgentsError, "tasks"):
            load_config(self.write("[plan]\n"))

    def test_invalid_toml_raises(self):
        with self.assertRaises(AgentsError):
            load_config(self.write("[plan\ntasks ="))

    def test_unknown_top_level_key_raises(self):
        with self.assertRaisesRegex(AgentsError, "未知的頂層鍵"):
            load_config(self.write(_MINIMAL + "[plann]\nx = 1\n"))

    def test_folder_name_with_space_rejected(self):
        with self.assertRaisesRegex(AgentsError, "工作資料夾名稱"):
            load_config(self.write('[plan]\ntasks = "my plan"\n'))

    def test_folder_name_with_slash_rejected(self):
        for bad in ("a/b", "a\\\\b"):
            with self.assertRaises(AgentsError):
                load_config(self.write(f'[plan]\ntasks = "{bad}"\n'))

    def test_folder_name_leading_dash_or_dot_rejected(self):
        for bad in ("-x", ".x"):
            with self.assertRaises(AgentsError):
                load_config(self.write(f'[plan]\ntasks = "{bad}"\n'))

    def test_invalid_folder_override_rejected(self):
        with self.assertRaisesRegex(AgentsError, "命令列工作資料夾"):
            load_config(self.write(_MINIMAL), folder="bad/name")

    def test_type_error_reported(self):
        with self.assertRaisesRegex(AgentsError, "型別錯誤"):
            load_config(self.write(_MINIMAL + "[watchdog]\nstall_minutes = \"x\"\n"))

    def test_negative_stall_rejected(self):
        with self.assertRaises(AgentsError):
            load_config(self.write(_MINIMAL + "[watchdog]\nstall_minutes = -1\n"))

    def test_models_table_full_replacement(self):
        cfg = load_config(self.write(
            _MINIMAL + '[adapter.claude.models]\nprime = "x"\n'))
        self.assertEqual(cfg.claude_models, {"prime": "x"})  # 整表取代,不合併

    def test_bad_default_effort_rejected(self):
        with self.assertRaisesRegex(AgentsError, "effort"):
            load_config(self.write(
                _MINIMAL + '[adapter.claude.default_effort]\nprime = "max"\n'))

    def test_prompt_template_loaded(self):
        cfg = load_config(self.write(_MINIMAL + '[prompt]\ntemplate = "hi {task_id}"\n'))
        self.assertEqual(cfg.prompt_template, "hi {task_id}")


if __name__ == "__main__":
    unittest.main()
