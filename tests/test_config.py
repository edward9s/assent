"""Tests for loading and validating assent.toml."""
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.config import (BUILTIN_LAYER, PROJECT_LAYER, USER_LAYER,
                           _ADAPTER_NAMES, list_task_folders, load_config,
                           validate_config)
from assent.user_home import ASSENT_HOME_ENV, user_assent_dir, user_config_path

_MINIMAL = ""


class ConfigTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # The user layer is redirected into a temp directory for every test in this
        # file: nothing here may read or write the developer's real ~/.assent.
        self.user_home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.user_home, ignore_errors=True)
        self.user_dir = self.user_home / ".assent"
        self.user_dir.mkdir()
        env = mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(self.user_dir)})
        env.start()
        self.addCleanup(env.stop)

    def write(self, text: str) -> Path:
        path = self.assent_dir / "assent.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def write_user(self, text: str) -> Path:
        path = self.user_dir / "assent.toml"
        path.write_text(text, encoding="utf-8")
        return path.resolve()

    @property
    def project_config(self) -> Path:
        """The project config path, which the caller supplies whether or not it exists."""
        return self.assent_dir / "assent.toml"


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
        # A wrong type is a type refusal; a blank string is an explicit-but-useless
        # value, refused by dotted key (see TestBlankOverrideSemantics).
        cases = (
            ('[adapter.claude.efforts]\nheavy = ""\n',
             r"adapter\.claude\.efforts\.heavy is blank"),
            ('[adapter.claude.efforts]\nnormal = 1\n',
             r"\[adapter\.claude\.efforts\].*non-empty string"),
            ('[adapter.codex.efforts.lite]\nslight = "   "\n',
             r"adapter\.codex\.efforts\.lite\.slight is blank"),
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


class TestUserHomePath(unittest.TestCase):
    def test_environment_override_wins_over_the_real_home(self):
        with mock.patch.dict(os.environ, {ASSENT_HOME_ENV: "/tmp/elsewhere"}):
            self.assertEqual(user_assent_dir(), Path("/tmp/elsewhere"))
            self.assertEqual(user_config_path(),
                             Path("/tmp/elsewhere") / "assent.toml")

    def test_unset_or_empty_override_falls_back_to_the_canonical_home(self):
        # Path computation only; the real home directory is never read or written.
        for value in (None, ""):
            with self.subTest(value=value):
                env = dict(os.environ)
                if value is None:
                    env.pop(ASSENT_HOME_ENV, None)
                else:
                    env[ASSENT_HOME_ENV] = value
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(user_assent_dir(), Path.home() / ".assent")
                    self.assertEqual(user_config_path(),
                                     Path.home() / ".assent" / "assent.toml")


class TestLayeredConfig(ConfigTestCase):
    def test_user_config_alone_loads_and_still_locates_the_project(self):
        user = self.write_user(
            '[adapter]\nname = "codex"\n'
            '[adapter.codex]\ncommand = "codex.cmd"\n'
            '[watchdog]\nstall_minutes = 7\n')
        cfg = load_config(self.project_config, "plan01")
        self.assertEqual(cfg.adapter_name, "codex")
        self.assertEqual(cfg.codex_command, "codex.cmd")
        self.assertEqual(cfg.stall_minutes, 7)
        # the missing project file is the project locator, not an error
        self.assertEqual(cfg.root, self.root.resolve())
        self.assertEqual(cfg.assent_dir, self.assent_dir.resolve())
        self.assertEqual(cfg.tasks_dir, self.assent_dir.resolve() / "plan01")
        self.assertEqual([source.layer for source in cfg.sources],
                         [BUILTIN_LAYER, USER_LAYER])
        self.assertEqual([source.path for source in cfg.sources], [None, user])
        self.assertEqual(validate_config(self.project_config),
                         self.assent_dir.resolve())

    def test_legacy_project_config_alone_still_loads(self):
        project = self.write('[run]\nretry_per_task = 4\n')
        cfg = load_config(project, "plan01")
        self.assertEqual(cfg.retry_per_task, 4)
        self.assertEqual([source.layer for source in cfg.sources],
                         [BUILTIN_LAYER, PROJECT_LAYER])
        self.assertEqual([source.path for source in cfg.sources],
                         [None, project.resolve()])

    def test_combined_layers_keep_user_values_and_take_project_overrides(self):
        user = self.write_user(
            '[watchdog]\nstall_minutes = 7\n'
            '[run]\nretry_per_task = 3\nquota_poll_minutes = 45\n')
        project = self.write('[run]\nretry_per_task = 5\n')
        cfg = load_config(project, "plan01")
        self.assertEqual(cfg.stall_minutes, 7)          # user-only key survives
        self.assertEqual(cfg.quota_poll_minutes, 45)    # sibling key untouched
        self.assertEqual(cfg.retry_per_task, 5)         # project override wins
        self.assertEqual(cfg.rotation_poll_minutes, 1)  # built-in default remains
        self.assertEqual([source.layer for source in cfg.sources],
                         [BUILTIN_LAYER, USER_LAYER, PROJECT_LAYER])
        self.assertEqual([source.path for source in cfg.sources],
                         [None, user, project.resolve()])

    def test_neither_layer_present_refuses_with_an_init_instruction(self):
        with self.assertRaises(AssentError) as raised:
            load_config(self.project_config, "plan01")
        message = str(raised.exception)
        self.assertIn(str(user_config_path()), message)
        self.assertIn(str(self.project_config.resolve()), message)
        self.assertIn("assent init", message)

    def test_partial_nested_project_override_wins_only_for_its_stated_keys(self):
        self.write_user(
            '[adapter.claude.models]\n'
            'prime = "user-prime"\ncore = "user-core"\nlite = "user-lite"\n'
            '[adapter.claude.default_effort]\nprime = "slight"\ncore = "slight"\n'
            '[adapter.claude.efforts.lite]\nheavy = "user-heavy"\nnormal = "user-normal"\n')
        cfg = load_config(self.write(
            '[adapter.claude.models]\ncore = "project-core"\n'
            '[adapter.claude.default_effort]\ncore = "normal"\n'
            '[adapter.claude.efforts.lite]\nheavy = "project-heavy"\n'), "plan01")
        self.assertEqual(cfg.claude_models,
                         {"prime": "user-prime", "core": "project-core",
                          "lite": "user-lite"})
        self.assertEqual(cfg.claude_default_effort,
                         {"prime": "slight", "core": "normal", "lite": "normal"})
        self.assertEqual(cfg.claude_tier_efforts,
                         {"lite": {"heavy": "project-heavy",
                                   "normal": "user-normal"}})

    def test_arrays_replace_rather_than_concatenate(self):
        self.write_user(
            '[adapter]\nname = ["claude", "codex", "antigravity"]\n'
            '[adapter.claude]\nextra_args = ["--user-a", "--user-b"]\n')
        cfg = load_config(self.write(
            '[adapter]\nname = ["codex"]\n'
            '[adapter.claude]\nextra_args = ["--project-only"]\n'), "plan01")
        self.assertEqual(cfg.adapter_names, ("codex",))
        self.assertEqual(cfg.adapter_name, "codex")
        self.assertEqual(cfg.claude_extra_args, ["--project-only"])

    def test_malformed_user_file_fails_with_its_own_path(self):
        user = self.write_user("[run\nretry_per_task =")
        self.write('[run]\nretry_per_task = 2\n')
        with self.assertRaises(AssentError) as raised:
            load_config(self.project_config, "plan01")
        message = str(raised.exception)
        self.assertIn("User config file is not valid TOML", message)
        self.assertIn(str(user), message)
        self.assertNotIn(str(self.project_config.resolve()), message)

    def test_malformed_project_file_fails_with_its_own_path(self):
        user = self.write_user('[run]\nretry_per_task = 2\n')
        project = self.write("[run\nretry_per_task =")
        with self.assertRaises(AssentError) as raised:
            load_config(project, "plan01")
        message = str(raised.exception)
        self.assertIn("Project config file is not valid TOML", message)
        self.assertIn(str(project.resolve()), message)
        self.assertNotIn(str(user), message)

    def test_unknown_top_level_key_names_the_layer_that_states_it(self):
        user = self.write_user("[plann]\nx = 1\n")
        self.write(_MINIMAL)
        with self.assertRaises(AssentError) as raised:
            load_config(self.project_config, "plan01")
        self.assertIn("User config file", str(raised.exception))
        self.assertIn(str(user), str(raised.exception))

        self.write_user(_MINIMAL)
        project = self.write("[plann]\nx = 1\n")
        with self.assertRaises(AssentError) as raised:
            load_config(project, "plan01")
        self.assertIn("Project config file", str(raised.exception))
        self.assertIn(str(project.resolve()), str(raised.exception))

    def test_incompatible_structures_across_layers_name_both_files(self):
        user = self.write_user('[adapter]\nname = "claude"\n')
        project = self.write('[adapter.name]\nprime = "claude"\n')
        with self.assertRaisesRegex(AssentError, r"adapter\.name.*incompatible"):
            load_config(project, "plan01")
        with self.assertRaises(AssentError) as raised:
            load_config(project, "plan01")
        self.assertIn(str(user), str(raised.exception))
        self.assertIn(str(project.resolve()), str(raised.exception))

    def test_provenance_names_the_layer_of_each_effective_setting(self):
        self.write_user(
            '[adapter]\nname = ["claude", "codex"]\n'
            '[adapter.claude.models]\nprime = "user-prime"\ncore = "user-core"\n'
            '[adapter.claude.default_effort]\nprime = "slight"\ncore = "slight"\n'
            '[adapter.claude.efforts.lite]\nheavy = "user-heavy"\nnormal = "user-normal"\n')
        cfg = load_config(self.write(
            '[adapter.claude.models]\ncore = "project-core"\n'
            '[adapter.claude.default_effort]\ncore = "normal"\n'
            '[adapter.claude.efforts.lite]\nheavy = "project-heavy"\n'), "plan01")
        expected = {
            "adapter.name": USER_LAYER,                        # adapter selection
            "adapter.claude.models.prime": USER_LAYER,         # model mappings
            "adapter.claude.models.core": PROJECT_LAYER,
            "adapter.claude.default_effort.prime": USER_LAYER,  # default efforts
            "adapter.claude.default_effort.core": PROJECT_LAYER,
            "adapter.claude.efforts.lite.normal": USER_LAYER,   # nested effort mappings
            "adapter.claude.efforts.lite.heavy": PROJECT_LAYER,
            # nothing states these, so the built-in default is the value in effect
            "adapter.codex.command": BUILTIN_LAYER,
            "watchdog.stall_minutes": BUILTIN_LAYER,
        }
        for key, layer in expected.items():
            with self.subTest(key=key):
                self.assertEqual(cfg.source_of(key), layer)

    def test_single_layer_provenance_and_default_source_chain(self):
        cfg = load_config(self.write(
            '[adapter]\nname = "codex"\n'
            '[adapter.codex.models]\nprime = "project-prime"\n'), "plan01")
        self.assertEqual(cfg.source_of("adapter.name"), PROJECT_LAYER)
        self.assertEqual(cfg.source_of("adapter.codex.models.prime"), PROJECT_LAYER)
        self.assertEqual(cfg.source_of("adapter.claude.models.prime"), BUILTIN_LAYER)
        # a worktree copy keeps the same answer about where a setting came from
        self.assertEqual(cfg.for_worktree(self.root).source_of("adapter.name"),
                         PROJECT_LAYER)


class TestBlankOverrideSemantics(ConfigTestCase):
    """Absence inherits; a blank value is explicit and never a hidden inherit request."""

    def test_omitted_project_key_and_empty_project_table_keep_the_user_value(self):
        self.write_user(
            '[adapter]\nname = "codex"\n'
            '[adapter.codex]\ncommand = "codex.cmd"\n'
            '[watchdog]\nstall_minutes = 7\n')
        # [adapter.codex] is stated but empty, [watchdog] is omitted entirely:
        # neither contributes a leaf override.
        project = self.write('[adapter.codex]\n[prompt]\n')
        cfg = load_config(project, "plan01")
        self.assertEqual(cfg.adapter_name, "codex")
        self.assertEqual(cfg.codex_command, "codex.cmd")
        self.assertEqual(cfg.stall_minutes, 7)
        # an empty table states no leaf, so provenance still points at the user file
        self.assertEqual(cfg.source_of("adapter.codex.command"), USER_LAYER)
        self.assertEqual(cfg.source_of("watchdog.stall_minutes"), USER_LAYER)
        self.assertEqual([source.layer for source in cfg.sources],
                         [BUILTIN_LAYER, USER_LAYER, PROJECT_LAYER])

    def test_empty_project_array_clears_the_user_array(self):
        self.write_user('[adapter.claude]\nextra_args = ["--user-a", "--user-b"]\n')
        cfg = load_config(
            self.write('[adapter.claude]\nextra_args = []\n'), "plan01")
        self.assertEqual(cfg.claude_extra_args, [])
        self.assertEqual(cfg.source_of("adapter.claude.extra_args"), PROJECT_LAYER)

    def test_empty_array_is_still_refused_where_the_schema_forbids_it(self):
        self.write_user('[adapter]\nname = ["claude", "codex"]\n')
        with self.assertRaisesRegex(AssentError, "non-empty list"):
            load_config(self.write('[adapter]\nname = []\n'), "plan01")

    def test_valueless_key_is_invalid_toml_naming_the_offending_file(self):
        self.write_user('[run]\nretry_per_task = 2\n')
        project = self.write("[adapter.claude]\ncommand =\n")
        with self.assertRaises(AssentError) as raised:
            load_config(project, "plan01")
        message = str(raised.exception)
        self.assertIn("Project config file is not valid TOML", message)
        self.assertIn(str(project.resolve()), message)

    def test_blank_operational_strings_fail_at_load_naming_key_and_source(self):
        # Every setting whose contract is "useful text": adapter selection, adapter
        # commands, model mappings, prompt templates, and effort translations.  Each is
        # refused while loading the config, not later when an adapter is launched.
        cases = (
            ('[adapter]\nname = ""\n', "adapter.name"),
            ('[adapter]\nname = ["claude", "  "]\n', "adapter.name"),
            ('[adapter.claude]\ncommand = "   "\n', "adapter.claude.command"),
            ('[adapter.codex.models]\ncore = ""\n', "adapter.codex.models.core"),
            ('[prompt]\ntemplate = "\\t"\n', "prompt.template"),
            ('[adapter.claude.efforts]\nnormal = ""\n',
             "adapter.claude.efforts.normal"),
            ('[adapter.antigravity.efforts.lite]\nheavy = " "\n',
             "adapter.antigravity.efforts.lite.heavy"),
        )
        for text, dotted in cases:
            with self.subTest(dotted=dotted, layer=PROJECT_LAYER):
                user = self.write_user(_MINIMAL)
                project = self.write(text)
                with self.assertRaises(AssentError) as raised:
                    load_config(project, "plan01")
                message = str(raised.exception)
                self.assertIn(f"Config {dotted} is blank", message)
                self.assertIn(PROJECT_LAYER, message)
                self.assertIn(str(project.resolve()), message)
                self.assertNotIn(str(user), message)
            with self.subTest(dotted=dotted, layer=USER_LAYER):
                user = self.write_user(text)
                self.write(_MINIMAL)
                with self.assertRaises(AssentError) as raised:
                    load_config(self.project_config, "plan01")
                message = str(raised.exception)
                self.assertIn(f"Config {dotted} is blank", message)
                self.assertIn(USER_LAYER, message)
                self.assertIn(str(user), message)

    def test_blank_project_value_never_falls_back_to_a_valid_user_value(self):
        user = self.write_user('[adapter.claude]\ncommand = "claude.cmd"\n')
        project = self.write('[adapter.claude]\ncommand = ""\n')
        with self.assertRaises(AssentError) as raised:
            load_config(project, "plan01")
        message = str(raised.exception)
        self.assertIn("adapter.claude.command is blank", message)
        self.assertIn(str(project.resolve()), message)
        self.assertNotIn(str(user), message)

    def test_enumerated_settings_keep_their_own_domain_refusal(self):
        # Blank handling must not displace the existing domain checks.
        with self.assertRaisesRegex(AssentError, "receipt_refresh"):
            load_config(self.write(
                '[verification]\nreceipt_refresh = ""\n'), "plan01")
        with self.assertRaisesRegex(AssentError, "is not a valid effort"):
            load_config(self.write(
                '[adapter.claude.default_effort]\ncore = ""\n'), "plan01")

    def test_project_file_of_only_empty_tables_changes_nothing(self):
        self.write_user(
            '[adapter]\nname = "claude"\n'
            '[adapter.claude.models]\ncore = "user-core"\n'
            '[adapter.claude.efforts.lite]\nheavy = "user-heavy"\n')
        cfg = load_config(self.write(
            "[adapter]\n[adapter.claude]\n[adapter.claude.models]\n"
            "[adapter.claude.efforts]\n[adapter.claude.efforts.lite]\n"
            "[run]\n[watchdog]\n[prompt]\n[verification]\n"), "plan01")
        # the user's stated models table still replaces the built-in one whole,
        # exactly as it does without the project file
        self.assertEqual(cfg.claude_models, {"core": "user-core"})
        self.assertEqual(cfg.claude_tier_efforts, {"lite": {"heavy": "user-heavy"}})
        self.assertEqual(cfg.claude_default_effort,
                         {"prime": "heavy", "core": "heavy", "lite": "normal"})
        self.assertEqual(cfg.source_of("adapter.claude.models.core"), USER_LAYER)
        self.assertEqual(cfg.source_of("adapter.claude.efforts.lite.heavy"),
                         USER_LAYER)
        self.assertEqual(cfg.receipt_refresh, "manual")


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
