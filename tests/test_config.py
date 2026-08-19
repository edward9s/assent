"""Tests for loading and validating assent.toml."""
import contextlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.config import (BUILTIN_LAYER, PROJECT_LAYER, USER_LAYER,
                           _ADAPTER_NAMES, list_task_plans, load_config,
                           validate_config, WorkflowActionStep)
from assent.init import init as run_init
from assent.user_home import ASSENT_HOME_ENV, user_assent_dir, user_config_path

_MINIMAL = ""
_WORKFLOW_ROLES = '''
[abilities.review]
prompt = "Review the plan."
writes = false
produces_verdict = true
[abilities.fix]
prompt = "Repair the durable findings."
writes = true
[abilities.observe]
prompt = "Observe only."
writes = false
[roles.reviewer]
ability = ["review"]
model = "prime"
effort = "heavy"
[roles.fixer]
ability = ["fix"]
model = "core"
[roles.reviewer_fixer]
ability = ["review", "fix"]
model = "prime"
effort = "heavy"
[roles.observer]
ability = ["observe"]
model = "core"
'''


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

    def write_adapter(self, text: str) -> Path:
        path = self.assent_dir / "adapter.toml"
        path.write_text(text, encoding="utf-8")
        return path

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
        self.assertEqual(cfg.stall_minutes, 0)   # watchdog off unless configured
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
        self.assertEqual(cfg.workflow_integration, ())

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
        self.assertEqual(cfg.auto_fix_state_rel,
                         ".assent/plan01/_auto_fix.toml")
        # The reviewed-shared-path manifest and its lock are Assent-owned local
        # execution memory in the project's .assent, so they are runtime
        # artifacts like the log and the receipt: never staged, never part of a
        # checkpoint, never a scope violation.
        self.assertEqual(cfg.git_excludes,
                         (".assent/plan01/_assent.log", ".assent/plan01/_report.md",
                         ".assent/plan01/assent.lock",
                         ".assent/plan01/_verification.toml",
                         ".assent/plan01/_auto_fix.toml",
                         ".assent/plan01/_workflow.toml",
                         ".assent/_integration_workflow.toml",
                         ".assent/manifest.toml", ".assent/manifest.lock"))

    def test_provided_plan_updates_all_derived_paths(self):
        cfg = load_config(self.write(_MINIMAL), plan_name="parallel02")
        self.assertEqual(cfg.tasks_name, "parallel02")
        self.assertEqual(cfg.tasks_dir, self.assent_dir.resolve() / "parallel02")
        self.assertEqual(cfg.branch_prefix, "parallel02/")
        self.assertEqual(cfg.runtime_log_rel, ".assent/parallel02/_assent.log")
        self.assertEqual(cfg.report_rel, ".assent/parallel02/_report.md")
        self.assertEqual(cfg.lockfile_rel, ".assent/parallel02/assent.lock")
        self.assertEqual(cfg.verification_receipt_rel,
                         ".assent/parallel02/_verification.toml")
        self.assertEqual(cfg.auto_fix_state_rel,
                         ".assent/parallel02/_auto_fix.toml")
        self.assertEqual(cfg.git_excludes,
                         (".assent/parallel02/_assent.log",
                          ".assent/parallel02/_report.md",
                          ".assent/parallel02/assent.lock",
                          ".assent/parallel02/_verification.toml",
                          ".assent/parallel02/_auto_fix.toml",
                          ".assent/parallel02/_workflow.toml",
                          ".assent/_integration_workflow.toml",
                          ".assent/manifest.toml", ".assent/manifest.lock"))

    def test_workflow_omitted_and_empty_boundaries(self):
        absent = load_config(self.write(_MINIMAL), "plan01")
        self.assertEqual(absent.workflow_plan, ())
        self.assertIsNone(absent.workflow_task)
        omitted = load_config(self.write("[workflow]\n"), "plan01")
        self.assertEqual(omitted.workflow_plan, ())
        self.assertIsNone(omitted.workflow_task)
        empty_plan = load_config(self.write(
            "[workflow]\nplan = []\n"), "plan01")
        self.assertEqual(empty_plan.workflow_plan, ())
        self.assertIsNone(empty_plan.workflow_task)
        empty_task = load_config(self.write(
            "[workflow]\ntask = []\n"), "plan01")
        self.assertEqual(empty_task.workflow_plan, ())
        self.assertEqual(empty_task.workflow_task, ())

    def test_workflow_builtin_actions_are_level_specific(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[workflow]\n'
            'task = [{ role = "fixer" }, { action = "focused_test" }]\n'
            'plan = [{ action = "focused_sweep" }]\n'
            'integration = [{ action = "full_verify" }]\n'), "plan01")

        self.assertEqual(cfg.workflow_task[0].role, "fixer")
        self.assertEqual(cfg.workflow_task[1], WorkflowActionStep("focused_test"))
        self.assertEqual(cfg.workflow_plan, (WorkflowActionStep("focused_sweep"),))
        self.assertEqual(
            cfg.workflow_integration, (WorkflowActionStep("full_verify"),))

    def test_task_workflow_adapter_may_be_fixed_rotated_or_inherited(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[adapter]\nname = ["claude", "codex"]\n'
            '[workflow]\ntask = ['
            '{ role = "fixer", adapter = "claude" }, '
            '{ role = "fixer", adapter = ["codex", "claude"] }, '
            '{ role = "fixer" }]\n'), "plan01")

        self.assertEqual(cfg.workflow_task[0].adapters, ("claude",))
        self.assertEqual(cfg.workflow_task[1].adapters, ("codex", "claude"))
        self.assertIsNone(cfg.workflow_task[2].adapters)

    def test_literal_role_values_require_one_workflow_adapter(self):
        role = (_WORKFLOW_ROLES
                + '[roles.literal_fixer]\n'
                  'ability = ["fix"]\n'
                  'model = "[Exact-Model]"\n'
                  'effort = "[XHigh]"\n'
                  '[adapter]\nname = ["claude", "codex"]\n')
        cfg = load_config(self.write(
            role + '[workflow]\ntask = ['
            '{ role = "literal_fixer", adapter = "codex" }]\n'), "plan01")
        resolved = cfg.workflow_task[0].resolved_role
        self.assertEqual(resolved.model, "[Exact-Model]")
        self.assertEqual(resolved.effort, "[XHigh]")

        with self.assertRaisesRegex(AssentError, "exactly one adapter"):
            load_config(self.write(
                role + '[workflow]\ntask = [{ role = "literal_fixer" }]\n'),
                "plan01")

    def test_workflow_entry_may_override_role_with_literal_values(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[workflow]\n'
            'task = [{ role = "fixer", adapter = "codex", '
            'model = "[Exact-Model]", effort = "[XHigh]" }]\n'), "plan01")

        resolved = cfg.workflow_task[0].resolved_role
        self.assertEqual(resolved.model, "[Exact-Model]")
        self.assertEqual(resolved.effort, "[XHigh]")

        with self.assertRaisesRegex(AssentError, "exactly one adapter"):
            load_config(self.write(
                _WORKFLOW_ROLES +
                '[adapter]\nname = ["claude", "codex"]\n'
                '[workflow]\ntask = [{ role = "fixer", '
                'model = "[Exact-Model]" }]\n'), "plan01")

    def test_plan_and_integration_roles_must_state_a_model(self):
        # A plan or integration session answers for a whole unit, so it has no
        # task to inherit a model from. Only workflow.task may omit it.
        roles = (
            '[abilities.fix]\nprompt = "Repair."\nwrites = true\n'
            '[abilities.review]\n'
            'prompt = "Review."\nwrites = false\nproduces_verdict = true\n'
            '[roles.reviewer]\nability = ["review"]\nmodel = "prime"\n'
            '[roles.bare_fixer]\nability = ["fix"]\n')
        for layer, action in (("plan", "focused_sweep"),
                              ("integration", "full_verify")):
            with self.subTest(layer=layer):
                with self.assertRaisesRegex(
                        AssentError,
                        r"role 'bare_fixer' must state model"):
                    load_config(self.write(
                        roles + '[workflow]\n'
                        f'{layer} = [{{ action = "{action}" }}, '
                        '{ role = "reviewer" }, { role = "bare_fixer" }, '
                        f'{{ action = "{action}" }}]\n'), "plan01")

        # The same model-less role stays valid inside workflow.task, which does
        # have a task to inherit from.
        cfg = load_config(self.write(
            roles + '[workflow]\ntask = [{ role = "bare_fixer" }]\n'), "plan01")
        self.assertIsNone(cfg.workflow_task[0].resolved_role.model)

        # Stating it on the workflow entry satisfies the rule without touching
        # the shared role definition.
        cfg = load_config(self.write(
            roles + '[workflow]\nplan = [{ action = "focused_sweep" }, '
            '{ role = "reviewer" }, '
            '{ role = "bare_fixer", model = "lite" }, '
            '{ action = "focused_sweep" }]\n'), "plan01")
        self.assertEqual(cfg.workflow_plan[2].model, "lite")

    def test_literal_verdict_role_may_omit_effort_for_vendor_default(self):
        cfg = load_config(self.write(
            '[abilities.review]\n'
            'prompt = "Review."\nwrites = false\nproduces_verdict = true\n'
            '[roles.literal_reviewer]\nability = ["review"]\n'
            '[workflow]\nintegration = ['
            '{ action = "full_verify" }, '
            '{ role = "literal_reviewer", adapter = "codex", '
            'model = "[Exact-Model]" }, '
            '{ action = "full_verify" }]\n'), "plan01")

        step = cfg.workflow_integration[1]
        self.assertEqual(step.requested_model, "Exact-Model")
        self.assertIsNone(step.effort)
        self.assertIsNone(step.requested_effort)

    def test_portable_verdict_role_may_use_adapter_default_effort(self):
        cfg = load_config(self.write(
            '[abilities.review]\n'
            'prompt = "Review."\nwrites = false\nproduces_verdict = true\n'
            '[roles.reviewer]\nability = ["review"]\nmodel = "core"\n'
            '[adapter]\nname = "codex"\n'
            '[workflow]\nplan = ['
            '{ action = "focused_sweep" }, { role = "reviewer" }, '
            '{ action = "focused_sweep" }]\n'), "plan01")

        step = cfg.workflow_plan[1]
        self.assertEqual(step.requested_model, "gpt-5.6-terra")
        self.assertIsNone(step.effort)
        self.assertEqual(step.requested_effort, "medium")

    def test_verdict_role_still_requires_an_effective_model(self):
        with self.assertRaisesRegex(AssentError, "must state model"):
            load_config(self.write(
                '[abilities.review]\n'
                'prompt = "Review."\nwrites = false\nproduces_verdict = true\n'
                '[roles.reviewer]\nability = ["review"]\n'
                '[workflow]\nplan = ['
                '{ action = "focused_sweep" }, { role = "reviewer" }, '
                '{ action = "focused_sweep" }]\n'), "plan01")

    def test_plan_and_integration_roles_accept_ordered_adapter_lists(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[workflow]\n'
            'plan = [{ action = "focused_sweep" }, '
            '{ role = "reviewer", adapter = ["codex", "claude"] }, '
            '{ action = "focused_sweep" }]\n'
            'integration = [{ action = "full_verify" }, '
            '{ role = "reviewer", adapter = ["claude", "codex"] }, '
            '{ action = "full_verify" }]\n'), "plan01")

        self.assertEqual(cfg.workflow_plan[1].adapters,
                         ("codex", "claude"))
        self.assertEqual(cfg.workflow_integration[1].adapters,
                         ("claude", "codex"))

    def test_task_workflow_adapter_rejects_invalid_values(self):
        cases = (
            ('[]', "non-empty string or list"),
            ('["claude", "claude"]', "must not contain duplicates"),
            ('["claude", 1]', "entries must be strings"),
            ('"unknown"', "not a registered adapter"),
            ('true', "must be a string or non-empty list"),
        )
        for adapter, message in cases:
            with self.subTest(adapter=adapter), self.assertRaisesRegex(
                    AssentError, message):
                load_config(self.write(
                    _WORKFLOW_ROLES + '[workflow]\ntask = ['
                    f'{{ role = "fixer", adapter = {adapter} }}]\n'), "plan01")

    def test_workflow_actions_reject_mixed_wrong_level_and_action_only_task(self):
        cases = (
            ('[workflow]\nplan = [{ role = "x", action = "focused_sweep" }]\n',
             "exactly one"),
            ('[workflow]\nplan = [{ action = "focused_sweep", adapter = "codex" }]\n',
             "unknown keys"),
            ('[workflow]\ntask = [{ action = "full_verify" }]\n',
             r"not valid.*task"),
            ('[workflow]\nplan = [{ action = "full_verify" }]\n',
             r"not valid.*plan"),
            ('[workflow]\nplan = [{ action = "focused_test" }]\n',
             r"not valid.*plan"),
            ('[workflow]\nintegration = [{ action = "focused_sweep" }]\n',
             r"not valid.*integration"),
            ('[workflow]\nintegration = [{ action = "deploy" }]\n',
             "unknown action"),
            ('[workflow]\ntask = [{ action = "full_test" }]\n',
             "unknown action"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(AssentError, message):
                load_config(self.write(text), "plan01")

    def test_workflow_integration_role_uses_plan_role_resolution(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[workflow]\nintegration = ['
            '{ action = "full_verify" }, '
            '{ role = "reviewer", adapter = "codex" }, '
            '{ role = "fixer", adapter = "claude" }, '
            '{ action = "full_verify" }]\n'), "plan01")

        step = cfg.workflow_integration[1]
        self.assertEqual(step.role, "reviewer")
        self.assertEqual(step.adapter, "codex")
        self.assertEqual(step.requested_model, "gpt-5.6-sol")
        self.assertEqual(step.requested_effort, "high")
        self.assertEqual(cfg.workflow_integration[2].role, "fixer")
        self.assertEqual(cfg.workflow_integration[2].adapter, "claude")

    def test_plan_is_the_only_per_plan_review_source(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[workflow]\nplan = [{ role = "reviewer_fixer", adapter = "codex" }, '
            '{ action = "focused_sweep" }, '
            '{ role = "reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }, '
            '{ role = "reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }]\n'
            'integration = [{ action = "full_verify" }]\n'), "plan01")

        self.assertEqual([step.role for step in cfg.workflow_plan
                          if not isinstance(step, WorkflowActionStep)],
                         ["reviewer_fixer", "reviewer", "reviewer"])

    def test_integration_full_verify_repair_positions_are_validated(self):
        cases = (
            ('{ role = "reviewer", adapter = "codex" }, '
             '{ action = "full_verify" }',
             "must start with full_verify"),
            ('{ action = "full_verify" }, { role = "fixer" }, '
             '{ action = "full_verify" }',
             "first role after full_verify.*must produce a verdict"),
            ('{ action = "full_verify" }, '
             '{ role = "reviewer", adapter = "codex" }, '
             '{ role = "reviewer", adapter = "codex" }, '
             '{ action = "full_verify" }',
             "optional fixer.*must write without producing a verdict"),
            ('{ action = "full_verify" }, '
             '{ role = "reviewer_fixer", adapter = "codex" }, '
             '{ role = "fixer", adapter = "codex" }, '
             '{ action = "full_verify" }',
             "writable verdict role.*must be the only role"),
            ('{ action = "full_verify" }, '
             '{ role = "reviewer", adapter = "codex" }',
             "must end with full_verify"),
        )
        for integration, message in cases:
            with self.subTest(integration=integration), self.assertRaisesRegex(
                    AssentError, message):
                load_config(self.write(
                    _WORKFLOW_ROLES + '[workflow]\nintegration = ['
                    + integration + ']\n'), "plan01")

    def test_workflow_verdict_step_reuses_adapter_mappings(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[adapter]\nname = "claude"\n'
            '[adapter.codex]\ncommand = "codex-review.cmd"\n'
            '[adapter.codex.models]\ncore = "review-model"\n'
            '[adapter.codex.efforts.core]\nslight = "review-effort"\n'
            '[roles.reviewer_core]\nability = ["review"]\nmodel = "core"\n'
            'effort = "slight"\n'
            '[workflow]\nplan = [{ action = "focused_sweep" }, '
            '{ role = "reviewer_core", adapter = "codex" }, '
            '{ action = "focused_sweep" }]\n'), "plan01")
        review = cfg.workflow_plan[1]
        self.assertEqual(review.adapter, "codex")
        self.assertNotIn(review.adapter, cfg.adapter_names)
        self.assertEqual(review.command, "codex-review.cmd")
        self.assertEqual(review.requested_model, "review-model")
        self.assertEqual(review.requested_effort, "review-effort")

    def test_workflow_rejects_invalid_shapes_and_values(self):
        cases = (
            ('auto_fix = true\n', "unknown top-level keys"),
            ('[auto_fix]\nextra = true\n', "unknown top-level keys"),
            ('workflow = true\n', r"\[workflow\].*table"),
            ('[workflow]\nextra = true\n', "unknown keys"),
            ('[workflow]\nplan = true\n', "wrong type"),
            (_WORKFLOW_ROLES + '[workflow]\nplan = [{ role = "reviewer", adapter = "unknown" }]\n',
             "not a registered adapter"),
            (_WORKFLOW_ROLES + '[workflow]\nplan = [{ role = "observer" }, '
             '{ action = "focused_sweep" }]\n',
             "first role before focused_sweep.*must produce a verdict"),
        )
        for text, message in cases:
            with self.subTest(text=text), self.assertRaisesRegex(AssentError, message):
                load_config(self.write(text), "plan01")

    def test_workflow_plan_roles_use_explicit_or_primary_adapter(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[workflow]\nplan = [{ action = "focused_sweep" }, '
            '{ role = "reviewer", adapter = "claude" }, '
            '{ role = "fixer" }, { action = "focused_sweep" }, '
            '{ role = "reviewer", adapter = "codex" }, '
            '{ action = "focused_sweep" }]\n'), "plan01")
        roles = [step for step in cfg.workflow_plan
                 if not isinstance(step, WorkflowActionStep)]
        self.assertEqual([step.role for step in roles],
                         ["reviewer", "fixer", "reviewer"])
        self.assertEqual([step.adapter for step in roles],
                         ["claude", "claude", "codex"])

    def test_verdict_and_fixer_roles_may_omit_or_override_adapter(self):
        cfg = load_config(self.write(
            _WORKFLOW_ROLES +
            '[adapter]\nname = ["claude", "codex"]\n'
            '[workflow]\nplan = [{ action = "focused_sweep" }, '
            '{ role = "reviewer" }, '
            '{ role = "fixer", adapter = "codex" }, '
            '{ action = "focused_sweep" }]\n'), "plan01")

        reviewer, fixer = cfg.workflow_plan[1:3]
        self.assertEqual(reviewer.adapter, "claude")
        self.assertEqual(reviewer.requested_model, "fable")
        self.assertEqual(reviewer.requested_effort, "high")
        self.assertEqual(fixer.adapter, "codex")

    def test_removed_auto_fix_review_names_table_and_layer_file(self):
        path = self.write('[auto_fix.review]\nadapter = "codex"\n')
        with self.assertRaisesRegex(AssentError, "unknown top-level keys"):
            load_config(path, "plan01")

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

    def test_portable_plan_names_are_accepted(self):
        for good in ("plan01", "selectedbatch01", "conflictreconcile01",
                     "sessionidentity01", "versionflag01", "alpha.beta",
                     "name-with-dash", "name_with_underscore", "v2"):
            with self.subTest(good=good):
                cfg = load_config(self.write(_MINIMAL), good)
                self.assertEqual(cfg.tasks_name, good)

    def test_git_and_windows_invalid_plan_names_are_rejected(self):
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
                self.assertIn("not a valid plan name", str(raised.exception))

    def test_invalid_plan_override_rejected(self):
        with self.assertRaisesRegex(AssentError, "Command-line plan"):
            load_config(self.write(_MINIMAL), plan_name="bad/name")

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

    def test_removed_workflow_settings_are_rejected(self):
        for text in (
                '[verification]\nreceipt_refresh = "manual"\n',
                '[workflow]\nselection = [{ action = "full_verify" }]\n',
                '[workflow]\nplan = [{ action = "full_test" }]\n'):
            with self.subTest(text=text), self.assertRaises(AssentError):
                load_config(self.write(text), "plan01")


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
    def test_project_adapter_file_matches_inline_adapter_layout(self):
        inline = self.write(
            '[adapter]\nname = ["codex"]\n'
            '[adapter.codex]\ncommand = "codex-test"\n')
        inline_cfg = load_config(inline, "plan01")

        self.write(_MINIMAL)
        split = self.write_adapter(
            '[adapter]\nname = ["codex"]\n'
            '[adapter.codex]\ncommand = "codex-test"\n')
        split_cfg = load_config(self.project_config, "plan01")

        self.assertEqual(split_cfg, inline_cfg)
        self.assertEqual(split.resolve(), self.assent_dir / "adapter.toml")

    def test_inline_and_split_adapter_tables_use_split_file_as_same_layer_overlay(self):
        self.write(
            '[adapter]\nname = "claude"\n'
            '[adapter.claude]\ncommand = "inline"\n')
        self.write_adapter(
            '[adapter.claude]\ncommand = "split"\n'
            'extra_args = ["--split"]\n')

        cfg = load_config(self.project_config, "plan01")

        self.assertEqual(cfg.adapter_name, "claude")
        self.assertEqual(cfg.claude_command, "split")
        self.assertEqual(cfg.claude_extra_args, ["--split"])

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

    def test_fresh_init_writes_adapter_tables_to_adapter_toml(self):
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_init(self.root, test="unittest"), 0)

        user_config = self.user_dir / "assent.toml"
        user_adapter = self.user_dir / "adapter.toml"
        config = tomllib.loads(user_config.read_text(encoding="utf-8"))
        adapter = tomllib.loads(user_adapter.read_text(encoding="utf-8"))
        self.assertNotIn("adapter", config)
        self.assertIn("adapter", adapter)
        cfg = load_config(self.project_config, "plan01")
        self.assertEqual(cfg.adapter_names, ("claude", "codex"))
        self.assertEqual(
            [step.action if isinstance(step, WorkflowActionStep) else "role"
             for step in cfg.workflow_task],
            ["role", "focused_test", "role", "focused_test"])
        self.assertEqual(
            [step.action if isinstance(step, WorkflowActionStep) else "role"
             for step in cfg.workflow_plan],
            ["role", "focused_sweep", "role", "focused_sweep", "role",
             "focused_sweep"])
        self.assertEqual(
            [step.action if isinstance(step, WorkflowActionStep) else "role"
             for step in cfg.workflow_integration],
            ["full_verify", "role", "full_verify"])

    def test_init_preserves_existing_inline_adapter_layout(self):
        templates = Path(__file__).resolve().parents[1] / "assent" / "templates"
        template = (
            templates.joinpath("assent.toml").read_text(encoding="utf-8").rstrip()
            + "\n\n"
            + templates.joinpath("adapter.toml").read_text(encoding="utf-8")
        ).encode("utf-8")
        user_config = self.user_dir / "assent.toml"
        user_config.write_bytes(template)
        before = user_config.read_bytes()
        subprocess.run(["git", "init"], cwd=self.root, check=True,
                       capture_output=True)
        output = io.StringIO()
        with mock.patch("builtins.input", return_value="n"), \
                contextlib.redirect_stdout(output):
            result = run_init(self.root)
        self.assertEqual(result, 0, output.getvalue())

        self.assertEqual(user_config.read_bytes(), before)
        self.assertFalse((self.user_dir / "adapter.toml").exists())


class TestBlankOverrideSemantics(ConfigTestCase):
    """Absence inherits; a blank value is explicit and never a hidden inherit request."""

    def test_omitted_project_key_and_empty_project_table_keep_the_user_value(self):
        self.write_user(
            '[adapter]\nname = "codex"\n'
            '[adapter.codex]\ncommand = "codex.cmd"\n'
            '[watchdog]\nstall_minutes = 7\n')
        # [adapter.codex] is stated but empty, [watchdog] is omitted entirely:
        # neither contributes a leaf override.
        project = self.write('[adapter.codex]\n')
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
        # commands, model mappings, and effort translations.  Each is refused while
        # loading the config, not later when an adapter is launched.
        cases = (
            ('[adapter]\nname = ""\n', "adapter.name"),
            ('[adapter]\nname = ["claude", "  "]\n', "adapter.name"),
            ('[adapter.claude]\ncommand = "   "\n', "adapter.claude.command"),
            ('[adapter.codex.models]\ncore = ""\n', "adapter.codex.models.core"),
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
        with self.assertRaisesRegex(AssentError, "unknown top-level keys"):
            load_config(self.write(
                '[verification]\nreceipt_refresh = ""\n'), "plan01")
        with self.assertRaisesRegex(AssentError, "is not a valid effort"):
            load_config(self.write(
                '[adapter.claude.default_effort]\ncore = ""\n'), "plan01")

    def test_project_file_of_only_empty_tables_changes_nothing(self):
        self.write_user(
            '[adapter]\nname = "claude"\n'
            '[adapter.claude.models]\nprime = "user-prime"\ncore = "user-core"\n'
            '[adapter.claude.efforts.lite]\nheavy = "user-heavy"\n')
        cfg = load_config(self.write(
            "[adapter]\n[adapter.claude]\n[adapter.claude.models]\n"
            "[adapter.claude.efforts]\n[adapter.claude.efforts.lite]\n"
            "[run]\n[watchdog]\n[workflow]\n"), "plan01")
        # the user's stated models table still replaces the built-in one whole,
        # exactly as it does without the project file
        self.assertEqual(cfg.claude_models,
                         {"prime": "user-prime", "core": "user-core"})
        self.assertEqual(cfg.claude_tier_efforts, {"lite": {"heavy": "user-heavy"}})
        self.assertEqual(cfg.claude_default_effort,
                         {"prime": "heavy", "core": "heavy", "lite": "normal"})
        self.assertEqual(cfg.source_of("adapter.claude.models.core"), USER_LAYER)
        self.assertEqual(cfg.source_of("adapter.claude.efforts.lite.heavy"),
                         USER_LAYER)


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
        self.assertEqual(codex.extra_args, ("--sandbox", "danger-full-access"))
        self.assertEqual(codex.resolve_model("prime"), "gpt-5.6-sol")
        with self.assertRaisesRegex(AssentError,
                                    r"\[adapter\.codex\.models\]"):
            codex.resolve_model("nonexistent")

    def test_literal_model_and_effort_bypass_adapter_mappings_independently(self):
        cfg = load_config(self.write(
            '[adapter.codex.efforts]\nheavy = "flat-heavy"\n'
            '[adapter.codex.efforts.prime]\nheavy = "tier-heavy"\n'),
            "plan01")
        codex = cfg.adapter_settings("codex")

        self.assertEqual(codex.resolve_model("[Exact-Model]"), "Exact-Model")
        self.assertIsNone(codex.resolve_effort(None, "[Exact-Model]"))
        self.assertEqual(
            codex.resolve_requested_effort("prime", "[XHigh]"), "XHigh")
        self.assertEqual(
            codex.resolve_requested_effort("[Exact-Model]", "heavy"),
            "flat-heavy")
        self.assertEqual(
            codex.resolve_requested_effort("[Exact-Model]", "[XHigh]"),
            "XHigh")

    def test_codex_builtin_extra_args_match_the_packaged_default(self):
        cfg = load_config(self.write(_MINIMAL), "plan01")
        template_path = (Path(__file__).resolve().parents[1]
                         / "assent" / "templates" / "adapter.toml")
        packaged = tomllib.loads(template_path.read_text(encoding="utf-8"))

        self.assertEqual(
            cfg.adapter_settings("codex").extra_args,
            tuple(packaged["adapter"]["codex"]["extra_args"]),
        )

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


class TestListTaskPlans(ConfigTestCase):
    def test_lists_only_visible_plans_containing_formal_task_files(self):
        for name, filename in (("beta", "t002_b.e.toml"),
                               ("alpha", "t001_a.e.toml"),
                               ("empty", "notes.txt"),
                               ("_hidden", "t001_h.e.toml"),
                               ("__pycache__", "t001_c.e.toml")):
            plan_name = self.assent_dir / name
            plan_name.mkdir()
            (plan_name / filename).write_text("", encoding="utf-8")
        self.assertEqual(list_task_plans(self.assent_dir), ["alpha", "beta"])

    def test_invalid_visible_live_plan_is_rejected(self):
        plan_name = self.assent_dir / "bad.lock"
        plan_name.mkdir()
        (plan_name / "t001_task.e.toml").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(AssentError, "bad\\.lock.*not a valid plan name"):
            list_task_plans(self.assent_dir)

    def test_discovers_unicode_task_filename_without_transliteration(self):
        plan_name = self.assent_dir / "unicode01"
        plan_name.mkdir()
        (plan_name / "t001_中文任務.e.toml").write_text(
            'title = "English task title"\n', encoding="utf-8")
        (plan_name / "t001_中文任務.r.toml").write_text(
            "[[entry]]\n", encoding="utf-8")
        self.assertEqual(list_task_plans(self.assent_dir), ["unicode01"])

    def test_missing_assent_directory_is_empty(self):
        self.assertEqual(list_task_plans(self.root / "missing"), [])


class TestAdapterRegistry(unittest.TestCase):
    def test_builtin_registry_holds_exactly_the_supported_adapters(self):
        # Set comparison, so this stays independent of declaration order and
        # fails both when a supported adapter drops out and when an unknown
        # name creeps in.
        self.assertEqual(_ADAPTER_NAMES, {"claude", "codex", "antigravity"})


if __name__ == "__main__":
    unittest.main()
