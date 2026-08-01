"""Antigravity adapter tests: hermetic, never a network call, a login or a real AGY session.

Every capability assertion is anchored to the recorded 1.1.5 evidence in
tests/fixtures/agy_models_1.1.5.txt and tests/fixtures/agy_selection_1.1.5.toml, so a later
CLI release that changes the contract fails here instead of failing during a paid run.
"""
import _thread
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.adapters import (CHECKPOINT_RESUME_RECORD, InvocationRequest,
                             TaskResult, get_adapter)
from assent.adapters.antigravity import (
    AntigravityAdapter, NAME, build_command, classify_output,
    format_output_line, load_catalog, log_file, parse_models_catalog,
    parse_version, recommended_effort, reserved_argument_errors,
)
from assent.adapters.process import run_subprocess
from assent.config import Config
from assent.plan import append_entry, read_entries

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOG_TEXT = (FIXTURES / "agy_models_1.1.5.txt").read_text(encoding="utf-8")
SELECTION = tomllib.loads(
    (FIXTURES / "agy_selection_1.1.5.toml").read_text(encoding="utf-8"))


def catalog():
    return parse_models_catalog(CATALOG_TEXT)


def make_cfg(**overrides) -> Config:
    values = dict(root=Path("."), assent_dir=Path("./.assent"),
                  tasks_dir=Path("./.assent/plan01"), tasks_name="plan01",
                  adapter_name=NAME)
    values.update(overrides)
    return Config(**values)


def make_adapter(**overrides) -> AntigravityAdapter:
    return AntigravityAdapter(make_cfg(**overrides), catalog=catalog())


def request(task_id="t001", tier="prime", effort="heavy", *, cfg=None):
    """Resolve one invocation exactly the way the engine does, from the config tables."""
    settings = (cfg or make_cfg()).adapter_settings(NAME)
    return InvocationRequest(
        task_id=task_id, model=tier, effort=effort,
        requested_model=settings.resolve_model(tier),
        requested_effort=settings.resolve_requested_effort(tier, effort))


class TestCapabilityCatalog(unittest.TestCase):
    def test_families_variants_and_standalone_are_derived_from_the_catalog(self):
        parsed = catalog()
        self.assertEqual(parsed.families, {
            "gemini-3.6-flash": ("low", "medium", "high"),
            "gemini-3.5-flash": ("low", "medium"),
            "gemini-3.1-pro": ("low", "high"),
        })
        # the bare 3.5 Flash slug is a family base, not a model that takes no effort
        self.assertNotIn("gemini-3.5-flash", parsed.standalone)
        self.assertEqual(parsed.standalone, ("gemini-3-flash",))
        self.assertEqual(parsed.variants["gemini-3.1-pro-high"], "high")

    def test_derived_rules_reproduce_every_recorded_cli_verdict(self):
        parsed = catalog()
        for probe in SELECTION["accepted"]:
            with self.subTest(accepted=probe):
                self.assertIsNone(
                    parsed.check(probe["model"], probe.get("effort")))
        for probe in SELECTION["rejected"]:
            with self.subTest(rejected=probe):
                reason = parsed.check(probe["model"], probe.get("effort"))
                self.assertIsNotNone(reason)

    def test_recorded_available_effort_sets_match_the_diagnostics(self):
        parsed = catalog()
        # The CLI names the effort set it does support; ours must name the same one, so a
        # diagnostic can be acted on without rerunning the CLI.
        self.assertIn("available: low, high",
                      SELECTION["rejected"][0]["message"])
        self.assertIn("low, high",
                      parsed.check("gemini-3.1-pro", "medium"))
        self.assertIn("low, medium",
                      parsed.check("gemini-3.5-flash", "high"))
        self.assertIn("low, medium, high",
                      parsed.check("gemini-3.6-flash", None))

    def test_unlisted_ai_studio_names_fail_closed_instead_of_downgrading(self):
        parsed = catalog()
        for name in ("gemini-3.5-flash-lite", "gemini-3.1-pro-preview",
                     "gemini-3.6-flash-preview"):
            with self.subTest(model=name):
                reason = parsed.check(name, "high")
                self.assertIn("not in this installation's AGY catalog", reason)

    def test_historical_observation_is_not_treated_as_the_current_catalog(self):
        # 3.1 Pro medium and a 3.5 Flash high slug were plausible from the older planning
        # notes; the current catalog proves neither exists, so neither may be sent.
        parsed = catalog()
        self.assertNotIn("gemini-3.5-flash-high", parsed.listed)
        self.assertNotIn("medium", parsed.families["gemini-3.1-pro"])
        self.assertIsNotNone(parsed.check("gemini-3.5-flash-high", None))

    def test_expanded_slug_conflicts_with_a_different_effort(self):
        parsed = catalog()
        self.assertIsNone(parsed.check("gemini-3.1-pro-high", "high"))
        self.assertIsNone(parsed.check("gemini-3.1-pro-high", None))
        self.assertIn("conflicts", parsed.check("gemini-3.1-pro-high", "low"))

    def test_empty_catalog_is_refused(self):
        with self.assertRaisesRegex(AssentError, "catalog is empty"):
            parse_models_catalog("\n  \n")

    def test_catalog_load_failure_is_an_assent_error(self):
        with mock.patch("assent.adapters.antigravity.subprocess.run",
                        side_effect=OSError("not installed")):
            with self.assertRaisesRegex(AssentError, "cannot run"):
                load_catalog("agy")


class TestVersionGate(unittest.TestCase):
    def test_version_parsing(self):
        self.assertEqual(parse_version("1.1.5"), (1, 1, 5))
        self.assertEqual(parse_version("agy version 2.0.10\n"), (2, 0, 10))
        self.assertIsNone(parse_version("unknown build"))

    def test_shipped_minimum_accepts_probed_version_and_refuses_older(self):
        adapter = make_adapter()
        cases = {"1.1.5": True, "1.2.0": True, "2.0.0": True,
                 "1.1.4": False, "1.0.9": False, "0.9.9": False}
        for banner, expected in cases.items():
            with self.subTest(version=banner), mock.patch(
                    "assent.adapters.Adapter.probe_cli",
                    return_value=(True, banner)):
                ok, message = adapter.probe_cli()
                self.assertEqual(ok, expected, message)
                if not expected:
                    self.assertIn("older than the required 1.1.5", message)

    def test_unparseable_banner_and_failed_probe_are_reported(self):
        adapter = make_adapter()
        with mock.patch("assent.adapters.Adapter.probe_cli",
                        return_value=(True, "unknown build")):
            ok, message = adapter.probe_cli()
        self.assertFalse(ok)
        self.assertIn("cannot read an agy version", message)

        with mock.patch("assent.adapters.Adapter.probe_cli",
                        return_value=(False, "executable not found 'agy'")):
            self.assertEqual(adapter.probe_cli(),
                             (False, "executable not found 'agy'"))


class TestBuildCommand(unittest.TestCase):
    def test_headless_flags_workspace_and_permissions(self):
        cfg = make_cfg()
        cmd = build_command(cfg, "the prompt", "gemini-3.1-pro", "high")
        self.assertEqual(cmd[:5],
                         ["agy", "--print", "the prompt", "--model", "gemini-3.1-pro"])
        self.assertEqual(cmd[cmd.index("--effort") + 1], "high")
        self.assertEqual(cmd[cmd.index("--mode") + 1], "accept-edits")
        self.assertEqual(cmd[cmd.index("--print-timeout") + 1], "120m")
        self.assertIn("--dangerously-skip-permissions", cmd)
        # the CLI log stays outside the isolated worktree
        self.assertNotIn(str(Path(".").resolve()),
                         cmd[cmd.index("--log-file") + 1])
        # the main tree's task folder is reachable, so the session can update its t/r files
        added = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "--add-dir"]
        self.assertIn(str(cfg.tasks_dir.resolve()), added)
        self.assertEqual(len(added), 2)      # task folder plus system temp
        # every element is a separate argument: nothing is ever handed to a shell
        self.assertTrue(all(isinstance(part, str) for part in cmd))
        self.assertEqual(sum(1 for part in cmd if part == "the prompt"), 1)

    def test_nine_cell_grid_has_exact_model_and_effort_flags(self):
        cfg = make_cfg()
        settings = cfg.adapter_settings(NAME)
        grid = {
            ("prime", "slight"): ("gemini-3.1-pro", "low"),
            ("prime", "normal"): ("gemini-3.1-pro", "high"),
            ("prime", "heavy"): ("gemini-3.1-pro", "high"),
            ("core", "slight"): ("gemini-3.6-flash", "low"),
            ("core", "normal"): ("gemini-3.6-flash", "medium"),
            ("core", "heavy"): ("gemini-3.6-flash", "high"),
            ("lite", "slight"): ("gemini-3.5-flash", "low"),
            ("lite", "normal"): ("gemini-3.5-flash", "medium"),
            ("lite", "heavy"): ("gemini-3.5-flash", "medium"),
        }
        parsed = catalog()
        for (tier, effort), (model, cli_effort) in grid.items():
            with self.subTest(tier=tier, effort=effort):
                requested_model = settings.resolve_model(tier)
                requested_effort = settings.resolve_requested_effort(tier, effort)
                self.assertEqual((requested_model, requested_effort),
                                 (model, cli_effort))
                cmd = build_command(cfg, "p", requested_model, requested_effort)
                self.assertEqual(cmd[cmd.index("--model") + 1], model)
                self.assertEqual(cmd[cmd.index("--effort") + 1], cli_effort)
                # a base slug never also carries an effort suffix, so the flag and the
                # model name can never contradict each other
                self.assertNotIn(f"-{cli_effort}", model)
                self.assertIsNone(parsed.check(requested_model, requested_effort))

    def test_effort_flag_is_omitted_when_no_effort_is_chosen(self):
        cmd = build_command(make_cfg(), "p", "gemini-3-flash", None)
        self.assertNotIn("--effort", cmd)

    def test_reserved_flags_are_refused_before_the_cli_starts(self):
        reserved = ["--model", "-p", "--print", "--effort", "--mode",
                    "--print-timeout", "--log-file", "--add-dir",
                    "--continue", "-c", "--conversation", "--agent",
                    "--prompt-interactive", "-i"]
        for flag in reserved:
            with self.subTest(flag=flag):
                cfg = make_cfg(antigravity_extra_args=[flag, "x"])
                with self.assertRaisesRegex(AssentError, "must not set"):
                    build_command(cfg, "p", "gemini-3.1-pro", "high")

    def test_reserved_flags_are_refused_in_joined_form_too(self):
        errors = reserved_argument_errors(["--model=gemini-3-flash"])
        self.assertEqual(len(errors), 1)
        self.assertIn("--model", errors[0])

    def test_legal_extra_args_are_passed_through_in_order(self):
        cfg = make_cfg(antigravity_extra_args=[
            "--sandbox", "--dangerously-skip-permissions"])
        cmd = build_command(cfg, "p", "gemini-3.1-pro", "high")
        self.assertEqual(cmd[-2:], ["--sandbox", "--dangerously-skip-permissions"])


class TestEffortPrecedence(unittest.TestCase):
    """The shared precedence rules keep working for the third adapter, unchanged."""

    def test_task_annotation_wins_over_the_tier_default(self):
        settings = make_cfg().adapter_settings(NAME)
        self.assertEqual(settings.resolve_effort("slight", "prime"), "slight")
        self.assertEqual(settings.resolve_effort(None, "prime"), "heavy")

    def test_translation_order_is_tier_then_flat_then_baseline(self):
        cfg = make_cfg(antigravity_efforts={"heavy": "flat-heavy"},
                       antigravity_tier_efforts={"prime": {"heavy": "tier-heavy"}})
        settings = cfg.adapter_settings(NAME)
        self.assertEqual(settings.resolve_requested_effort("prime", "heavy"),
                         "tier-heavy")
        self.assertEqual(settings.resolve_requested_effort("core", "heavy"),
                         "flat-heavy")
        self.assertEqual(settings.resolve_requested_effort("core", "slight"), "low")

    def test_omitted_effort_sends_no_flag_and_invents_no_cli_default(self):
        cfg = make_cfg(antigravity_default_effort={},
                       antigravity_models={"lite": "gemini-3-flash"})
        settings = cfg.adapter_settings(NAME)
        effort = settings.resolve_effort(None, "lite")
        self.assertIsNone(effort)
        self.assertIsNone(settings.resolve_requested_effort("lite", effort))
        cmd = build_command(cfg, "p", settings.resolve_model("lite"), None)
        self.assertNotIn("--effort", cmd)
        self.assertIsNone(catalog().check("gemini-3-flash", None))

    def test_expanded_slug_never_pairs_with_a_contradictory_effort_flag(self):
        # Configuring an expanded slug is legal, but only without a conflicting effort.
        matching = make_cfg(antigravity_models={"prime": "gemini-3.1-pro-high"},
                            antigravity_default_effort={})
        adapter = AntigravityAdapter(matching, catalog=catalog())
        settings = matching.adapter_settings(NAME)
        self.assertEqual(adapter.preflight([
            InvocationRequest(task_id="t001", model="prime", effort=None,
                              requested_model=settings.resolve_model("prime"),
                              requested_effort=None)]), [])

        conflicting = make_cfg(
            antigravity_models={"prime": "gemini-3.1-pro-high"},
            antigravity_tier_efforts={"prime": {"slight": "low"}})
        errors = AntigravityAdapter(conflicting, catalog=catalog()).preflight(
            [request("t001", "prime", "slight", cfg=conflicting)])
        self.assertEqual(len(errors), 1)
        self.assertIn("conflicts with --effort=low", errors[0])
        self.assertIn("[adapter.antigravity.models] prime", errors[0])


class TestPreflight(unittest.TestCase):
    def test_shipped_mapping_passes_for_every_tier(self):
        adapter = make_adapter()
        requests = [request(f"t00{i}", tier, effort)
                    for i, (tier, effort) in enumerate(
                        [(t, e) for t in ("prime", "core", "lite")
                         for e in ("slight", "normal", "heavy")], start=1)]
        self.assertEqual(adapter.preflight(requests), [])

    def test_wrong_pro_medium_mapping_names_the_exact_owner_and_fix(self):
        cfg = make_cfg(antigravity_tier_efforts={"prime": {"normal": "medium"}})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t007", "prime", "normal", cfg=cfg)])

        self.assertEqual(len(errors), 1)
        message = errors[0]
        self.assertIn("t007", message)
        self.assertIn("--model gemini-3.1-pro --effort medium", message)
        self.assertIn("available: low, high", message)
        self.assertIn("[adapter.antigravity.efforts.prime] normal = \"high\"", message)
        self.assertIn("current value 'medium'", message)

    def test_preflight_diagnostic_keeps_abstract_key_and_suggests_vendor_value(self):
        cfg = make_cfg(antigravity_tier_efforts={"prime": {"slight": "medium"}})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t008", "prime", "slight", cfg=cfg)])

        self.assertEqual(len(errors), 1)
        message = errors[0]
        self.assertIn("[adapter.antigravity.efforts.prime] slight = \"high\"", message)
        self.assertIn("current value 'medium'", message)

    def test_preflight_spends_no_subprocess_when_a_catalog_is_supplied(self):
        adapter = make_adapter()
        with mock.patch("assent.adapters.antigravity.subprocess.run",
                        side_effect=AssertionError("the CLI must not be started")):
            self.assertEqual(adapter.preflight([request()]), [])

    def test_unmapped_model_points_at_the_models_table(self):
        cfg = make_cfg(antigravity_models={"prime": "gemini-3.5-flash-lite"})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t001", "prime", "heavy", cfg=cfg)])
        self.assertEqual(len(errors), 1)
        self.assertIn("not in this installation's AGY catalog", errors[0])
        self.assertIn("[adapter.antigravity.models] prime", errors[0])

    def test_effortless_model_with_an_effort_is_refused(self):
        cfg = make_cfg(antigravity_models={"lite": "gemini-3-flash"})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t002", "lite", "heavy", cfg=cfg)])
        self.assertEqual(len(errors), 1)
        self.assertIn("supports no --effort at all", errors[0])

    def test_family_base_without_any_effort_is_refused(self):
        cfg = make_cfg(antigravity_default_effort={})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([
            InvocationRequest(task_id="t003", model="core", effort=None,
                              requested_model="gemini-3.6-flash",
                              requested_effort=None)])
        self.assertEqual(len(errors), 1)
        self.assertIn("requires an effort", errors[0])
        self.assertIn("[adapter.antigravity.default_effort] core", errors[0])

    def test_reserved_extra_args_fail_the_preflight_too(self):
        cfg = make_cfg(antigravity_extra_args=["--model", "gemini-3-flash"])
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request(cfg=cfg)])
        self.assertTrue(any("must not set --model" in e for e in errors))

    def test_unavailable_catalog_fails_closed(self):
        adapter = AntigravityAdapter(make_cfg())
        with mock.patch("assent.adapters.antigravity.subprocess.run",
                        side_effect=OSError("not installed")):
            errors = adapter.preflight([request()])
        self.assertEqual(len(errors), 1)
        self.assertIn("capability catalog is unavailable", errors[0])

    def test_duplicate_diagnostics_are_reported_once(self):
        cfg = make_cfg(antigravity_tier_efforts={"prime": {"normal": "medium"}})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([
            request("t001", "prime", "normal", cfg=cfg),
            request("t001", "prime", "normal", cfg=cfg)])
        self.assertEqual(len(errors), 1)

    def test_recommendation_is_quality_first_then_the_family_ceiling(self):
        self.assertEqual(recommended_effort(("low", "high"), "medium"), "high")
        self.assertEqual(recommended_effort(("low", "medium"), "high"), "medium")
        self.assertEqual(recommended_effort(("low", "medium", "high"), "low"), "low")

    def test_recommendation_uses_vendor_order_for_a_translated_effort(self):
        self.assertEqual(recommended_effort(("medium", "high"), "low"), "medium")


class TestOutputContract(unittest.TestCase):
    def test_plain_text_is_shown_without_inventing_events(self):
        self.assertEqual(format_output_line("done\n"), "  AI| done")
        self.assertIn("!|", format_output_line("Error: something broke"))
        self.assertIsNone(format_output_line("   \n"))
        # a line that merely looks structured is still only a line of text
        rendered = format_output_line('{"type": "result", "usage": {"output_tokens": 5}}')
        self.assertTrue(rendered.startswith("  AI| "))
        self.assertIn('{"type": "result"', rendered)

    def test_checkpoint_resume_record_is_hidden_from_live_output(self):
        self.assertIsNone(format_output_line(CHECKPOINT_RESUME_RECORD + "\n"))


class TestCheckpointResume(unittest.TestCase):
    def test_exact_final_record_is_recognized(self):
        from assent.adapters import parse_checkpoint_resume_output

        output = "partial\n" + CHECKPOINT_RESUME_RECORD + "\n\n"
        self.assertTrue(parse_checkpoint_resume_output(output, 1, False))

    def test_zero_exit_stall_and_nonfinal_or_lookalike_records_are_rejected(self):
        from assent.adapters import parse_checkpoint_resume_output

        cases = (
            (0, CHECKPOINT_RESUME_RECORD + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD + "\n", True),
            (1, "prefix" + CHECKPOINT_RESUME_RECORD + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD[:-1] + "\n", False),
            (1, CHECKPOINT_RESUME_RECORD + "\ntrailing\n", False),
            (1, CHECKPOINT_RESUME_RECORD + " \n", False),
            (1, '{"type": "assent.checkpoint_resume"}\n', False),
        )
        for exit_code, output, stalled in cases:
            with self.subTest(exit_code=exit_code, output=output, stalled=stalled):
                self.assertFalse(
                    parse_checkpoint_resume_output(output, exit_code, stalled))

    def test_failure_classification(self):
        cases = {
            "Error: invalid model selection (--model \"x\")": "unsupported_model",
            "Error: Resource has been exhausted (e.g. check quota).": "quota",
            "Error: quota exceeded for this project": "quota",
            "Error: your credit balance is too low": "billing",
            "Error: insufficient funds on the account": "billing",
            "Error: permission denied for tool write_to_file": "permission",
            "Error: Agent Platform API has not been used in project": "permission",
            "Error: timed out waiting for the response": "timeout",
            "Error: transport closed unexpectedly": "nonzero",
        }
        for output, expected in cases.items():
            with self.subTest(output=output):
                self.assertEqual(classify_output(1, False, output), expected)
        self.assertIsNone(classify_output(0, False, "all good"))
        # a watchdog kill is a task failure, never quota, whatever the text said
        self.assertEqual(classify_output(1, True, "rate limit exceeded"), "stall")


class TestRunTask(unittest.TestCase):
    def patch_run(self, fake):
        import assent.adapters.antigravity as module
        original = module.run_subprocess
        module.run_subprocess = fake
        self.addCleanup(setattr, module, "run_subprocess", original)

    def test_command_uses_resolved_values_and_reports_success(self):
        captured = {}

        def fake(command, cwd, stall_seconds, echo=None, heartbeat_path=None):
            captured.update(command=command, cwd=cwd, stall_seconds=stall_seconds,
                            heartbeat_path=heartbeat_path)
            return 0, "finished\n", False

        self.patch_run(fake)
        adapter = make_adapter()
        result = adapter.run_task("p", adapter.resolve_model("core"), "medium",
                                  Path("/work"))
        self.assertIsInstance(result, TaskResult)
        self.assertEqual(captured["command"][captured["command"].index("--model") + 1],
                         "gemini-3.6-flash")
        self.assertEqual(captured["cwd"], Path("/work"))
        self.assertEqual(captured["stall_seconds"], 30 * 60)
        self.assertEqual(captured["heartbeat_path"], log_file(make_cfg()))
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.failure_kind)
        self.assertFalse(result.quota_exhausted)

    def test_log_file_is_removed_after_success_failure_and_stall(self):
        for outcome, fake_result in (
                ("success", (0, "done\n", False)),
                ("failure", (2, "Error: boom", False)),
                ("stall", (1, "", True))):
            with self.subTest(outcome=outcome):
                cfg = make_cfg(tasks_name=f"plan01_{outcome}")
                log_path = log_file(cfg)
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text("internal detail\n", encoding="utf-8")
                self.patch_run(lambda *a, **k: fake_result)
                adapter = AntigravityAdapter(cfg, catalog=catalog())
                adapter.run_task("p", "gemini-3.1-pro", "high", Path("."))
                self.assertFalse(log_path.exists())

    def test_log_file_is_removed_even_when_interrupted(self):
        cfg = make_cfg(tasks_name="plan01_interrupt")
        log_path = log_file(cfg)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("secret\n", encoding="utf-8")

        def fake(*a, **k):
            raise KeyboardInterrupt

        self.patch_run(fake)
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        with self.assertRaises(KeyboardInterrupt):
            adapter.run_task("p", "gemini-3.1-pro", "high", Path("."))
        self.assertFalse(log_path.exists())

    def test_nonzero_exit_is_never_disguised_as_success(self):
        self.patch_run(lambda *a, **k: (2, "Error: transport closed", False))
        result = make_adapter().run_task("p", "gemini-3.1-pro", "high", Path("."))
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.failure_kind, "nonzero")
        self.assertFalse(result.quota_exhausted)

    def test_quota_and_stall_outcomes(self):
        self.patch_run(
            lambda *a, **k: (1, "Error: Resource has been exhausted", False))
        quota = make_adapter().run_task("p", "gemini-3.1-pro", "high", Path("."))
        self.assertTrue(quota.quota_exhausted)
        self.assertEqual(quota.failure_kind, "quota")
        self.assertIsNone(quota.reset_at)   # print mode states none, so none is invented

        self.patch_run(
            lambda *a, **k: (1, "Error: Resource has been exhausted", True))
        stalled = make_adapter().run_task("p", "gemini-3.1-pro", "high", Path("."))
        self.assertTrue(stalled.stalled)
        self.assertFalse(stalled.quota_exhausted)
        self.assertEqual(stalled.failure_kind, "stall")

    def test_billing_outcome_is_classified_and_is_not_quota(self):
        # Print mode emits prose, so the billing phrase is matched against the transcript;
        # it must classify as billing (fail fast) and never as quota (wait-and-resume).
        self.patch_run(
            lambda *a, **k: (1, "Error: your credit balance is too low", False))
        result = make_adapter().run_task("p", "gemini-3.1-pro", "high", Path("."))
        self.assertEqual(result.failure_kind, "billing")
        self.assertFalse(result.quota_exhausted)
        self.assertIsNone(result.reset_at)

    def test_checkpoint_resume_record_sets_distinct_result_and_keeps_raw_output(self):
        output = "partial\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = make_adapter().run_task(
            "p", "gemini-3.1-pro", "high", Path("."))
        self.assertTrue(result.checkpoint_resume)
        self.assertFalse(result.quota_exhausted)
        self.assertEqual(result.output, output)
        self.assertIsNone(result.failure_kind)

    def test_quota_and_control_record_use_the_quota_path(self):
        output = "Error: Resource has been exhausted\n" + CHECKPOINT_RESUME_RECORD + "\n"
        self.patch_run(lambda *args, **kwargs: (1, output, False))
        result = make_adapter().run_task(
            "p", "gemini-3.1-pro", "high", Path("."))
        self.assertTrue(result.quota_exhausted)
        self.assertFalse(result.checkpoint_resume)

    def test_terminal_record_overrides_preceding_non_quota_classifiers(self):
        for prose in (
                "Error: invalid model selection",
                "Error: your credit balance is too low",
                "Error: permission denied for tool write_to_file",
                "Error: timed out waiting for the response"):
            with self.subTest(prose=prose):
                output = prose + "\n" + CHECKPOINT_RESUME_RECORD + "\n"
                self.patch_run(lambda *args, output=output, **kwargs:
                               (1, output, False))
                result = make_adapter().run_task(
                    "p", "gemini-3.1-pro", "high", Path("."))
                self.assertTrue(result.checkpoint_resume)
                self.assertFalse(result.quota_exhausted)
                self.assertIsNone(result.failure_kind)

    def test_unsupported_model_outcome_is_classified_for_the_scheduler(self):
        self.patch_run(lambda *a, **k: (
            1, 'Error: invalid model selection (--model "gemini-3.1-pro" '
               '--effort "medium"): gemini-3.1-pro has no "medium" effort', False))
        result = make_adapter().run_task("p", "gemini-3.1-pro", "medium", Path("."))
        self.assertEqual(result.failure_kind, "unsupported_model")
        self.assertFalse(result.quota_exhausted)


class TestRegistrationAndJournal(unittest.TestCase):
    def test_get_adapter_returns_the_antigravity_adapter(self):
        adapter = get_adapter(NAME, make_cfg())
        self.assertIsInstance(adapter, AntigravityAdapter)
        self.assertEqual(adapter.settings.name, NAME)
        self.assertEqual(adapter.resolve_model("prime"), "gemini-3.1-pro")

    def test_unknown_adapter_is_still_refused(self):
        with self.assertRaisesRegex(AssentError, "unknown adapter: 'nowhere'"):
            get_adapter("nowhere", make_cfg())

    def test_journal_accepts_antigravity_and_still_reads_legacy_entries(self):
        import tempfile
        journal = Path(tempfile.mkdtemp()) / "t001_task.r.toml"
        journal.write_text(
            '[[entry]]\ntime = "2026-07-01T00:00:00+00:00"\n'
            'by = "ai"\nevent = "done"\nsummary = "legacy entry stays readable"\n',
            encoding="utf-8")
        append_entry(journal, by=NAME, requested_model="gemini-3.1-pro",
                     requested_effort="high", event="done", summary="finished")
        append_entry(journal, by="scheduler", agent=NAME,
                     requested_model="gemini-3.1-pro", requested_effort="high",
                     event="blocked", summary="scheduler verdict")

        entries = read_entries(journal)
        self.assertEqual([e["by"] for e in entries], ["ai", NAME, "scheduler"])
        self.assertEqual(entries[1]["requested_model"], "gemini-3.1-pro")
        self.assertEqual(entries[1]["requested_effort"], "high")
        self.assertEqual(entries[2]["agent"], NAME)

    def test_journal_still_refuses_an_unregistered_identity(self):
        import tempfile
        journal = Path(tempfile.mkdtemp()) / "t001_task.r.toml"
        with self.assertRaisesRegex(AssentError, "by is invalid"):
            append_entry(journal, by="nowhere", event="done", summary="x")


# --------------------------------------------------------------------------- #
# Real fake-CLI subprocess reliability (t003)
#
# Every test below spawns tests/fixtures/fake_agy.py as a genuine child process --
# `sys.executable` in front of it, since a bare .py file is not directly executable on
# Windows -- so the no-shell argv list, real stdout/stderr merge, watchdog timing and
# process-kill mechanics of assent.adapters.process.run_subprocess are exercised for real.
# Nothing here touches the network, a real login or a real model: FAKE_AGY_SCENARIO
# selects the outcome, so these tests are fully hermetic and deterministic.
# --------------------------------------------------------------------------- #
FAKE_AGY = FIXTURES / "fake_agy.py"


def _fake_agy_command(prompt, *, model="gemini-3.1-pro", effort="high",
                      log_path=None, print_timeout="1m", extra=()):
    cmd = [sys.executable, str(FAKE_AGY), "--print", prompt, "--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--mode", "accept-edits", "--print-timeout", print_timeout]
    if log_path is not None:
        cmd += ["--log-file", str(log_path)]
    cmd += list(extra)
    return cmd


def _pid_running(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class RealSubprocessTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_scenario(self, scenario, prompt="do the thing", *, stall_seconds=10,
                     heartbeat_path=None, echo=None, env=None, **kw):
        full_env = {"FAKE_AGY_SCENARIO": scenario, **(env or {})}
        with mock.patch.dict(os.environ, full_env):
            command = _fake_agy_command(prompt, **kw)
            return run_subprocess(command, self.tmp, stall_seconds,
                                  echo=echo, heartbeat_path=heartbeat_path)


class TestFakeCLIClassification(RealSubprocessTestCase):
    """Real, hermetic child-process runs of every outcome the adapter must classify."""

    def test_success_is_not_a_failure(self):
        rc, out, stalled = self.run_scenario("success")
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIsNone(classify_output(rc, stalled, out))

    def test_normal_reply_containing_quota_word_is_not_misread(self):
        rc, out, stalled = self.run_scenario("success_with_quota_word")
        self.assertEqual(rc, 0)
        self.assertIn("quota", out.lower())
        # exit 0 prose that merely mentions quota/limit is never itself a quota failure
        self.assertIsNone(classify_output(rc, stalled, out))

    def test_stderr_error_is_classified_nonzero(self):
        rc, out, stalled = self.run_scenario("stderr_error")
        self.assertNotEqual(rc, 0)
        self.assertIn("transport closed", out)
        self.assertEqual(classify_output(rc, stalled, out), "nonzero")

    def test_permission_soft_deny_is_classified(self):
        rc, out, stalled = self.run_scenario("permission")
        self.assertEqual(classify_output(rc, stalled, out), "permission")

    def test_quota_signal_is_classified(self):
        rc, out, stalled = self.run_scenario("quota")
        self.assertEqual(classify_output(rc, stalled, out), "quota")

    def test_unsupported_model_is_classified(self):
        rc, out, stalled = self.run_scenario("unsupported_model")
        self.assertEqual(classify_output(rc, stalled, out), "unsupported_model")

    def test_print_timeout_text_is_classified_timeout_not_stall(self):
        rc, out, stalled = self.run_scenario("print_timeout")
        # AGY's own bound firing is a classified failure, never the assent watchdog kill
        self.assertFalse(stalled)
        self.assertEqual(classify_output(rc, stalled, out), "timeout")


class TestFakeCLIUnicodeAndPrompt(RealSubprocessTestCase):
    def test_unicode_whitespace_path_and_multiline_prompt_round_trip(self):
        odd_dir = self.tmp / "工作 目錄 with spaces"
        odd_dir.mkdir()
        prompt = ("多行 prompt 第一行\n"
                  'Second line with "quotes" and a tab\there\n'
                  "第三行:percent %PATH%, amp &, pipe |, caret ^\n")
        with mock.patch.dict(os.environ, {"FAKE_AGY_SCENARIO": "echo_roundtrip"}):
            command = _fake_agy_command(prompt, extra=["--add-dir", str(odd_dir)])
            rc, out, stalled = run_subprocess(command, odd_dir, stall_seconds=10)
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertIn(f"PROMPT_LEN={len(prompt)}", out)
        self.assertIn(repr(prompt), out)
        self.assertIn(repr(str(odd_dir)), out)


class TestFakeCLILiveEcho(RealSubprocessTestCase):
    def test_lines_arrive_incrementally_not_only_at_the_end(self):
        timestamps: list[float] = []
        rc, out, stalled = self.run_scenario(
            "streaming", stall_seconds=10, echo=lambda line: timestamps.append(
                time.monotonic()),
            env={"FAKE_AGY_TICK_SECONDS": "0.15"})
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)
        self.assertEqual(len(timestamps), 3)
        # if every line had only arrived once the process was already finished, the three
        # timestamps would be clustered together instead of spread over ~0.3s
        self.assertGreater(timestamps[-1] - timestamps[0], 0.2)


class TestWatchdogHeartbeat(RealSubprocessTestCase):
    """The behaviour that makes a print-mode CLI's own log file count as activity."""

    def test_silent_but_alive_process_is_not_killed_when_heartbeat_is_fresh(self):
        log_path = self.tmp / "agy.log"
        rc, out, stalled = self.run_scenario(
            "silent_alive", stall_seconds=0.6, heartbeat_path=log_path,
            log_path=log_path,
            env={"FAKE_AGY_TICKS": "5", "FAKE_AGY_TICK_SECONDS": "0.15"})
        self.assertFalse(stalled)
        self.assertEqual(rc, 0)
        self.assertIn("Done after a long silent-but-alive stretch", out)

    def test_same_silence_without_a_heartbeat_path_is_killed(self):
        # No heartbeat_path given: behaves exactly like the pre-existing claude/codex
        # watchdog, which only ever counted a stdout line as activity.
        log_path = self.tmp / "agy.log"
        rc, out, stalled = self.run_scenario(
            "silent_alive", stall_seconds=0.3, heartbeat_path=None,
            log_path=log_path,
            env={"FAKE_AGY_TICKS": "5", "FAKE_AGY_TICK_SECONDS": "0.15"})
        self.assertTrue(stalled)

    def test_genuinely_silent_process_is_still_killed_with_a_heartbeat_path(self):
        log_path = self.tmp / "agy.log"
        start = time.monotonic()
        rc, out, stalled = self.run_scenario(
            "silent_dead", stall_seconds=0.4, heartbeat_path=log_path,
            log_path=log_path, env={"FAKE_AGY_HANG_SECONDS": "30"})
        elapsed = time.monotonic() - start
        self.assertTrue(stalled)
        self.assertLess(elapsed, 10)   # killed promptly, not left running the full 30s hang


class TestPrintTimeoutAndWatchdogPrecedence(RealSubprocessTestCase):
    def test_watchdog_disabled_never_produces_an_immediate_timeout(self):
        rc, out, stalled = self.run_scenario("success", stall_seconds=0)
        self.assertEqual(rc, 0)
        self.assertFalse(stalled)

    def test_agys_own_timeout_and_the_assent_watchdog_are_independent_signals(self):
        # AGY's own bound firing is a classified failure, not a watchdog kill.
        rc, out, stalled = self.run_scenario("print_timeout", stall_seconds=30)
        self.assertFalse(stalled)
        self.assertEqual(classify_output(rc, stalled, out), "timeout")

        # A truly stuck process is the watchdog's own kill, whatever it did or didn't print.
        rc2, out2, stalled2 = self.run_scenario(
            "hang", stall_seconds=0.3, env={"FAKE_AGY_HANG_SECONDS": "30"})
        self.assertTrue(stalled2)


class TestRealChildInterruptCleanup(unittest.TestCase):
    def test_keyboard_interrupt_kills_the_real_child_process(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pidfile = tmp / "pid.txt"
        env = {"FAKE_AGY_SCENARIO": "hang", "FAKE_AGY_PIDFILE": str(pidfile),
              "FAKE_AGY_HANG_SECONDS": "30"}

        def trigger():
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and not pidfile.exists():
                time.sleep(0.02)
            time.sleep(0.1)   # small grace so the child is inside its sleep loop
            _thread.interrupt_main()

        timer = threading.Thread(target=trigger, daemon=True)
        timer.start()
        with mock.patch.dict(os.environ, env):
            command = _fake_agy_command("go")
            with self.assertRaises(KeyboardInterrupt):
                run_subprocess(command, tmp, stall_seconds=5)
        timer.join(timeout=2)

        self.assertTrue(pidfile.exists(), "the child never started")
        pid = int(pidfile.read_text().strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_running(pid):
            time.sleep(0.1)
        self.assertFalse(_pid_running(pid), "the child process was not cleaned up")


if __name__ == "__main__":
    unittest.main()
