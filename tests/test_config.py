"""Tests for loading and validating assent.toml."""
import shutil
import tempfile
import unittest
from pathlib import Path

from assent import AssentError
from assent.config import _ADAPTER_NAMES, list_task_folders, load_config

_MINIMAL = ""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, text: str) -> Path:
        path = self.assent_dir / "assent.toml"
        path.write_text(text, encoding="utf-8")
        return path


class TestLoadConfig(ConfigTestCase):
    def test_minimal_config_and_defaults(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(cfg.root, self.root.resolve())
        self.assertEqual(cfg.assent_dir, self.assent_dir.resolve())
        self.assertEqual(cfg.tasks_name, "plan01")
        self.assertEqual(cfg.tasks_dir, self.assent_dir.resolve() / "plan01")
        self.assertEqual(cfg.branch_prefix, "plan01/")
        self.assertEqual(cfg.stall_minutes, 30)
        self.assertEqual(cfg.retry_per_task, 1)
        self.assertEqual(cfg.quota_poll_minutes, 30)
        self.assertEqual(cfg.rotation_poll_minutes, 1)
        self.assertEqual(cfg.adapter_name, "claude")
        self.assertEqual(cfg.adapter_names, ("claude",))
        self.assertEqual(cfg.claude_models["prime"], "fable")
        self.assertEqual(cfg.codex_models["lite"], "gpt-5.6-luna")
        self.assertEqual(cfg.claude_efforts, {})
        self.assertEqual(cfg.claude_tier_efforts, {})
        self.assertEqual(cfg.codex_efforts, {})
        self.assertEqual(cfg.codex_tier_efforts, {})
        self.assertIsNone(cfg.prompt_template)
        self.assertEqual(cfg.receipt_refresh, "manual")

    def test_scalar_and_singleton_adapter_name_are_equivalent(self):
        scalar = load_config(self.write('[adapter]\nname = "claude"\n'), "plan01")
        singleton = load_config(
            self.write('[adapter]\nname = ["claude"]\n'), "plan01")
        self.assertEqual(scalar.adapter_names, ("claude",))
        self.assertEqual(singleton.adapter_names, ("claude",))
        self.assertEqual(scalar.adapter_name, singleton.adapter_name)
        self.assertEqual(scalar.adapter_name, singleton.adapter_names[0])

    def test_adapter_name_rotation_list_preserves_order(self):
        cfg = load_config(self.write(
            '[adapter]\nname = ["codex", "antigravity", "claude"]\n'), "plan01")
        self.assertEqual(cfg.adapter_names, ("codex", "antigravity", "claude"))
        self.assertEqual(cfg.adapter_name, "codex")

    def test_adapter_name_rotation_list_rejects_empty_unknown_duplicate_and_bad_type(self):
        cases = (
            ('[adapter]\nname = []\n', "non-empty"),
            ('[adapter]\nname = ["nowhere"]\n', "unknown adapter name"),
            ('[adapter]\nname = ["claude", "claude"]\n', "duplicate"),
            ('[adapter]\nname = 1\n', "wrong type"),
            ('[adapter]\nname = ["claude", 1]\n', r"name\[1\].*string"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(AssentError, message):
                load_config(self.write(text), "plan01")

    def test_rotation_poll_minutes_default_custom_and_invalid_values(self):
        self.assertEqual(load_config(self.write(_MINIMAL), "plan01").rotation_poll_minutes, 1)
        cfg = load_config(self.write(
            "[run]\nrotation_poll_minutes = 7\n"), "plan01")
        self.assertEqual(cfg.rotation_poll_minutes, 7)
        for value in (0, -1):
            with self.subTest(value=value), self.assertRaisesRegex(
                    AssentError, "rotation_poll_minutes"):
                load_config(self.write(
                    f"[run]\nrotation_poll_minutes = {value}\n"), "plan01")
        with self.assertRaisesRegex(AssentError, "wrong type"):
            load_config(self.write(
                '[run]\nrotation_poll_minutes = "1"\n'), "plan01")

    def test_antigravity_defaults_match_the_probed_agy_capability(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(cfg.antigravity_command, "agy")
        self.assertEqual(cfg.antigravity_extra_args,
                         ["--dangerously-skip-permissions"])
        self.assertEqual(cfg.antigravity_models,
                         {"prime": "gemini-3.1-pro", "core": "gemini-3.6-flash",
                          "lite": "gemini-3.5-flash"})
        # every tier defaults to a heavy investment; the vendor translation below is what
        # keeps that request sendable for families with a lower ceiling
        self.assertEqual(cfg.antigravity_default_effort,
                         {"prime": "heavy", "core": "heavy", "lite": "heavy"})
        self.assertEqual(cfg.antigravity_efforts, {})
        self.assertEqual(cfg.antigravity_tier_efforts,
                         {"prime": {"normal": "high"}, "lite": {"heavy": "medium"}})
        self.assertEqual(cfg.antigravity_print_timeout_minutes, 120)

    def test_shipped_template_loads_with_expected_adapter_settings(self):
        template = (Path(__file__).resolve().parents[1]
                    / "assent" / "templates" / "assent.toml")
        cfg = load_config(self.write(template.read_text(encoding="utf-8")),
                          "template01")
        tiers = {"prime", "core", "lite"}
        efforts = {"slight", "normal", "heavy"}
        for adapter in ("claude", "codex", "antigravity"):
            with self.subTest(adapter=adapter):
                settings = cfg.adapter_settings(adapter)
                self.assertEqual(set(settings.models), tiers)
                self.assertEqual(set(settings.default_effort), tiers)
                self.assertTrue(set(settings.default_effort.values()) <= efforts)
        self.assertEqual(cfg.antigravity_print_timeout_minutes, 120)

    def test_antigravity_effort_table_is_replaced_whole_not_merged(self):
        cfg = load_config(self.write(
            '[adapter.antigravity.efforts.prime]\nnormal = "medium"\n'), "plan01")
        self.assertEqual(cfg.antigravity_tier_efforts,
                         {"prime": {"normal": "medium"}})

    def test_antigravity_print_timeout_must_be_positive(self):
        with self.assertRaisesRegex(AssentError, "print_timeout_minutes"):
            load_config(self.write(
                '[adapter.antigravity]\nprint_timeout_minutes = 0\n'), "plan01")

    def test_runtime_artifact_paths(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(cfg.runtime_log_rel, ".assent/plan01/_assent.log")
        self.assertEqual(cfg.report_rel, ".assent/plan01/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".assent/plan01/assent.lock")
        self.assertEqual(cfg.verification_receipt_rel,
                         ".assent/plan01/_verification.toml")
        self.assertEqual(cfg.git_excludes,
                         (".assent/plan01/_assent.log", ".assent/plan01/_report.md",
                         ".assent/plan01/assent.lock",
                         ".assent/plan01/_verification.toml"))

    def test_provided_folder_updates_all_derived_paths(self):
        cfg = load_config(self.write(_MINIMAL), folder="parallel02")
        self.assertEqual(cfg.tasks_name, "parallel02")
        self.assertEqual(cfg.tasks_dir, self.assent_dir.resolve() / "parallel02")
        self.assertEqual(cfg.branch_prefix, "parallel02/")
        self.assertEqual(cfg.runtime_log_rel, ".assent/parallel02/_assent.log")
        self.assertEqual(cfg.report_rel, ".assent/parallel02/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".assent/parallel02/assent.lock")
        self.assertEqual(cfg.verification_receipt_rel,
                         ".assent/parallel02/_verification.toml")
        self.assertEqual(cfg.git_excludes,
                         (".assent/parallel02/_assent.log",
                          ".assent/parallel02/_report.md",
                          ".assent/parallel02/assent.lock",
                          ".assent/parallel02/_verification.toml"))

    def test_missing_file_raises(self):
        with self.assertRaises(AssentError):
            load_config(self.assent_dir / "assent.toml", "plan01")

    def test_removed_plan_section_rejected_as_unknown_key(self):
        with self.assertRaisesRegex(AssentError, "unknown top-level keys"):
            load_config(self.write('[plan]\ntasks = "plan01"\n'), "plan01")

    def test_invalid_toml_raises(self):
        with self.assertRaises(AssentError):
            load_config(self.write("[run\nretry_per_task ="), "plan01")

    def test_unknown_top_level_key_raises(self):
        with self.assertRaisesRegex(AssentError, "unknown top-level keys"):
            load_config(self.write("[plann]\nx = 1\n"), "plan01")

    def test_removed_git_section_rejected_as_unknown_key(self):
        with self.assertRaisesRegex(AssentError, "unknown top-level keys"):
            load_config(self.write("[git]\nenabled = false\n"), "plan01")

    def test_portable_folder_names_are_accepted(self):
        for good in ("plan01", "selectedbatch01", "conflictreconcile01",
                     "sessionidentity01", "versionflag01", "alpha.beta",
                     "name-with-dash", "name_with_underscore", "v2"):
            with self.subTest(good=good):
                cfg = load_config(self.write(_MINIMAL), good)
                self.assertEqual(cfg.tasks_name, good)

    def test_git_and_windows_invalid_folder_names_are_rejected(self):
        invalid = (
            "", "my plan", "a/b", "a\\b", "-x", ".x",
            "bad\x00name", "bad\x01name", "bad\x7fname", "bad~name",
            "bad^name", "bad:name",
            "bad?name", "bad*name", "bad[name", "bad<name", "bad>name",
            'bad"name', "bad|name", "bad..name", "bad@{name", "bad.",
            "bad.lock", "bad.LOCK", "CON", "con.txt", "PrN.log", "AUX",
            "nul.data", "COM1", "lpt9.txt", "COM¹", "LPT³")
        for bad in invalid:
            with self.subTest(bad=bad):
                with self.assertRaises(AssentError) as raised:
                    load_config(self.write(_MINIMAL), bad)
                self.assertIn(repr(bad), str(raised.exception))
                self.assertIn("not a valid task folder name", str(raised.exception))

    def test_invalid_folder_override_rejected(self):
        with self.assertRaisesRegex(AssentError, "Command-line task folder"):
            load_config(self.write(_MINIMAL), folder="bad/name")

    def test_type_error_reported(self):
        with self.assertRaisesRegex(AssentError, "wrong type"):
            load_config(self.write("[watchdog]\nstall_minutes = \"x\"\n"), "plan01")

    def test_negative_stall_rejected(self):
        with self.assertRaises(AssentError):
            load_config(self.write("[watchdog]\nstall_minutes = -1\n"), "plan01")

    def test_models_table_full_replacement(self):
        cfg = load_config(self.write(
            '[adapter.claude.models]\nprime = "x"\n'), "plan01")
        self.assertEqual(cfg.claude_models, {"prime": "x"})  # whole table replaced, not merged

    def test_bad_default_effort_rejected(self):
        with self.assertRaisesRegex(AssentError, "effort"):
            load_config(self.write(
                '[adapter.claude.default_effort]\nprime = "high"\n'), "plan01")

    def test_efforts_flat_and_tier_sections_loaded(self):
        cfg = load_config(self.write(
            '[adapter.codex.efforts]\n'
            'heavy = "minimal"\nnormal = "balanced"\n'
            '[adapter.codex.efforts.lite]\n'
            'slight = "max"\n'), "plan01")
        self.assertEqual(cfg.codex_efforts,
                         {"heavy": "minimal", "normal": "balanced"})
        self.assertEqual(cfg.codex_tier_efforts,
                         {"lite": {"slight": "max"}})

    def test_bad_efforts_keys_and_section_names_rejected(self):
        cases = (
            ('[adapter.claude.efforts]\nlow = "x"\n',
             r"\[adapter\.claude\.efforts\].*low"),
            ('[adapter.codex.efforts.ultra]\nheavy = "x"\n',
             r"\[adapter\.codex\.efforts\].*ultra"),
            ('[adapter.codex.efforts.lite]\nhigh = "x"\n',
             r"\[adapter\.codex\.efforts\.lite\].*high"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(
                    AssentError, message):
                load_config(self.write(text), "plan01")

    def test_bad_efforts_values_rejected(self):
        cases = (
            ('[adapter.claude.efforts]\nheavy = ""\n',
             r"\[adapter\.claude\.efforts\].*non-empty string"),
            ('[adapter.claude.efforts]\nnormal = 1\n',
             r"\[adapter\.claude\.efforts\].*non-empty string"),
            ('[adapter.codex.efforts.lite]\nslight = "   "\n',
             r"\[adapter\.codex\.efforts\.lite\].*non-empty string"),
            ('[adapter.codex.efforts.lite]\nheavy = false\n',
             r"\[adapter\.codex\.efforts\.lite\].*non-empty string"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(
                    AssentError, message):
                load_config(self.write(text), "plan01")

    def test_prompt_template_loaded(self):
        cfg = load_config(self.write(
            '[prompt]\ntemplate = "hi {task_id}"\n'), "plan01")
        self.assertEqual(cfg.prompt_template, "hi {task_id}")

    def test_receipt_refresh_domain_default_and_fail_closed(self):
        # An absent section, an absent key, and each stated mode; anything else is
        # refused at load time rather than silently treated as one of the two.
        self.assertEqual(
            load_config(self.write("[verification]\n"), "plan01").receipt_refresh,
            "manual")
        for mode in ("manual", "auto"):
            with self.subTest(mode=mode):
                cfg = load_config(self.write(
                    f'[verification]\nreceipt_refresh = "{mode}"\n'), "plan01")
                self.assertEqual(cfg.receipt_refresh, mode)
        with self.assertRaisesRegex(AssentError, "receipt_refresh"):
            load_config(self.write(
                '[verification]\nreceipt_refresh = "always"\n'), "plan01")
        with self.assertRaisesRegex(AssentError, "wrong type"):
            load_config(self.write(
                "[verification]\nreceipt_refresh = true\n"), "plan01")


class TestAdapterSettings(ConfigTestCase):
    def test_unknown_adapter_is_rejected_not_claude_fallback(self):
        # An unregistered adapter name must fail closed here, never silently inherit
        # another vendor's mapping.
        cfg = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(cfg.adapter_settings("claude").models["prime"], "fable")
        self.assertEqual(cfg.adapter_settings("codex").models["lite"],
                         "gpt-5.6-luna")
        self.assertEqual(cfg.adapter_settings("antigravity").models["prime"],
                         "gemini-3.1-pro")
        with self.assertRaisesRegex(AssentError, "unknown adapter: 'nowhere'"):
            cfg.adapter_settings("nowhere")

    def test_settings_carry_vendor_specific_command_and_maps(self):
        cfg = load_config(self.write(
            '[adapter.codex]\ncommand = "codex.cmd"\n'
            '[adapter.codex.efforts]\nheavy = "minimal"\nnormal = "balanced"\n'
            '[adapter.codex.efforts.lite]\nslight = "max"\n'), "plan01")
        codex = cfg.adapter_settings("codex")
        self.assertEqual(codex.name, "codex")
        self.assertEqual(codex.command, "codex.cmd")
        self.assertEqual(codex.extra_args, ("--sandbox", "workspace-write"))
        self.assertEqual(codex.resolve_model("prime"), "gpt-5.6-sol")
        with self.assertRaisesRegex(AssentError,
                                    r"\[adapter\.codex\.models\]"):
            codex.resolve_model("nonexistent")

    def test_resolve_effort_precedence_task_then_stated_default_then_builtin(self):
        cfg = load_config(self.write(
            '[adapter.claude.default_effort]\n'
            'prime = "slight"\ncore = "normal"\n'), "plan01")
        settings = cfg.adapter_settings("claude")
        # task annotation wins over the tier default
        self.assertEqual(settings.resolve_effort("slight", "prime"), "slight")
        # a stated tier default applies when the task omits effort
        self.assertEqual(settings.resolve_effort(None, "prime"), "slight")
        self.assertEqual(settings.resolve_effort(None, "core"), "normal")
        # the partial table overrides only the tiers it names; lite keeps the built-in
        self.assertEqual(settings.resolve_effort(None, "lite"), "normal")

    def test_default_effort_table_overrides_per_tier_never_suppresses(self):
        # An absent, empty, and partial table all keep the complete built-in defaults,
        # so no tier falls through to whatever the vendor CLI would have picked.
        builtin = {"prime": "heavy", "core": "heavy", "lite": "normal"}
        for text in ("", '[adapter.claude.default_effort]\n',
                     '[adapter.claude.default_effort]\ncore = "slight"\n'):
            with self.subTest(text=text):
                cfg = load_config(self.write(text), "plan01")
                settings = cfg.adapter_settings("claude")
                expected = dict(builtin)
                if "core =" in text:
                    expected["core"] = "slight"
                self.assertEqual(settings.default_effort, expected)
                for model, effort in expected.items():
                    self.assertEqual(settings.resolve_effort(None, model), effort)
                    self.assertEqual(
                        settings.resolve_requested_effort(model, effort),
                        {"heavy": "high", "normal": "medium",
                         "slight": "low"}[effort])

    def test_default_effort_keeps_builtin_defaults_for_every_adapter(self):
        cfg = load_config(self.write(
            '[adapter.codex.default_effort]\n'
            '[adapter.antigravity.default_effort]\nlite = "slight"\n'), "plan01")
        self.assertEqual(cfg.adapter_settings("codex").default_effort,
                         {"prime": "heavy", "core": "normal", "lite": "slight"})
        self.assertEqual(cfg.adapter_settings("antigravity").default_effort,
                         {"prime": "heavy", "core": "heavy", "lite": "slight"})

    def test_bad_default_effort_tier_key_rejected(self):
        with self.assertRaisesRegex(
                AssentError,
                r"\[adapter\.claude\.default_effort\].*'ultra'.*model tier"):
            load_config(self.write(
                '[adapter.claude.default_effort]\nultra = "heavy"\n'), "plan01")
        with self.assertRaisesRegex(AssentError, "wrong type"):
            load_config(self.write(
                '[adapter.claude]\ndefault_effort = "heavy"\n'), "plan01")

    def test_requested_effort_grid_tier_then_flat_then_baseline(self):
        cfg = load_config(self.write(
            '[adapter.claude.efforts]\n'
            'heavy = "minimal"\nnormal = "balanced"\n'   # no flat "slight" -> baseline
            '[adapter.claude.efforts.lite]\nheavy = "tiny"\n'), "plan01")
        settings = cfg.adapter_settings("claude")
        grid = {
            ("prime", "heavy"): "minimal", ("prime", "normal"): "balanced",
            ("prime", "slight"): "low",
            ("core", "heavy"): "minimal", ("core", "normal"): "balanced",
            ("core", "slight"): "low",
            ("lite", "heavy"): "tiny",      # tier-specific beats flat
            ("lite", "normal"): "balanced",
            ("lite", "slight"): "low",     # built-in baseline fallback
        }
        for (model, effort), expected in grid.items():
            with self.subTest(model=model, effort=effort):
                self.assertEqual(
                    settings.resolve_requested_effort(model, effort), expected)

    def test_requested_effort_without_table_uses_builtin_baseline(self):
        settings = load_config(self.write(_MINIMAL), "plan01").adapter_settings("claude")
        self.assertEqual(settings.resolve_requested_effort("core", "heavy"), "high")
        self.assertEqual(settings.resolve_requested_effort("core", "normal"), "medium")
        self.assertEqual(settings.resolve_requested_effort("core", "slight"), "low")

    def test_requested_effort_flat_mapping_falls_back_per_key_to_baseline(self):
        settings = load_config(self.write(
            '[adapter.claude.efforts]\nheavy = "custom-heavy"\n'),
            "plan01").adapter_settings("claude")
        self.assertEqual(settings.resolve_requested_effort("core", "heavy"), "custom-heavy")
        self.assertEqual(settings.resolve_requested_effort("core", "normal"), "medium")
        self.assertEqual(settings.resolve_requested_effort("core", "slight"), "low")

    def test_requested_effort_precedence_is_tier_then_flat_then_baseline(self):
        settings = load_config(self.write(
            '[adapter.claude.efforts]\nheavy = "flat-heavy"\n'
            '[adapter.claude.efforts.lite]\nheavy = "tier-heavy"\n'),
            "plan01").adapter_settings("claude")
        self.assertEqual(settings.resolve_requested_effort("lite", "heavy"), "tier-heavy")
        self.assertEqual(settings.resolve_requested_effort("core", "heavy"), "flat-heavy")
        self.assertEqual(settings.resolve_requested_effort("core", "normal"), "medium")


    def test_antigravity_shipped_grid_is_complete_and_monotone(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        settings = cfg.adapter_settings("antigravity")
        grid = {
            # Gemini 3.1 Pro exposes low and high only: normal goes up, quality first.
            ("prime", "slight"): ("gemini-3.1-pro", "low"),
            ("prime", "normal"): ("gemini-3.1-pro", "high"),
            ("prime", "heavy"): ("gemini-3.1-pro", "high"),
            ("core", "slight"): ("gemini-3.6-flash", "low"),
            ("core", "normal"): ("gemini-3.6-flash", "medium"),
            ("core", "heavy"): ("gemini-3.6-flash", "high"),
            # AGY exposes no Flash Lite, so lite uses 3.5 Flash, whose ceiling is medium.
            ("lite", "slight"): ("gemini-3.5-flash", "low"),
            ("lite", "normal"): ("gemini-3.5-flash", "medium"),
            ("lite", "heavy"): ("gemini-3.5-flash", "medium"),
        }
        for (tier, effort), expected in grid.items():
            with self.subTest(tier=tier, effort=effort):
                self.assertEqual(
                    (settings.resolve_model(tier),
                     settings.resolve_requested_effort(tier, effort)),
                    expected)
        # an omitted task effort still lands on the tier default, which is heavy everywhere
        for tier in ("prime", "core", "lite"):
            self.assertEqual(settings.resolve_effort(None, tier), "heavy")


class TestListTaskFolders(ConfigTestCase):
    def test_lists_only_visible_folders_containing_formal_task_files(self):
        for name, filename in (("beta", "t002_b.e.toml"),
                               ("alpha", "t001_a.e.toml"),
                               ("empty", "notes.txt"),
                               ("_hidden", "t001_h.e.toml"),
                               ("__pycache__", "t001_c.e.toml")):
            folder = self.assent_dir / name
            folder.mkdir()
            (folder / filename).write_text("", encoding="utf-8")
        self.assertEqual(list_task_folders(self.assent_dir), ["alpha", "beta"])

    def test_invalid_visible_live_folder_is_rejected(self):
        folder = self.assent_dir / "bad.lock"
        folder.mkdir()
        (folder / "t001_task.e.toml").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "bad\\.lock.*not a valid task folder name"):
            list_task_folders(self.assent_dir)

    def test_missing_assent_directory_is_empty(self):
        self.assertEqual(list_task_folders(self.root / "missing"), [])


class TestAdapterRegistry(unittest.TestCase):
    def test_builtin_registry_holds_exactly_the_supported_adapters(self):
        # Set comparison, so this stays independent of declaration order and
        # fails both when a supported adapter drops out and when an unknown
        # name creeps in.
        self.assertEqual(_ADAPTER_NAMES, {"claude", "codex", "antigravity"})


if __name__ == "__main__":
    unittest.main()
