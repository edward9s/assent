"""Antigravity adapter tests: hermetic, never a network call, a login or a real AGY session.

Every capability assertion is anchored to the recorded 1.1.5 evidence in
tests/fixtures/agy_models_1.1.5.txt and tests/fixtures/agy_selection_1.1.5.toml, so a later
CLI release that changes the contract fails here instead of failing during a paid run.
"""
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from assent import AssentError
from assent.adapters import InvocationRequest, TaskResult, get_adapter
from assent.adapters.antigravity import (
    AntigravityAdapter, NAME, build_command, classify_output,
    format_output_line, load_catalog, parse_models_catalog, parse_version,
    recommended_effort, reserved_argument_errors,
)
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


def request(task_id="t001", tier="prime", effort="high", *, cfg=None):
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
            ("prime", "low"): ("gemini-3.1-pro", "low"),
            ("prime", "medium"): ("gemini-3.1-pro", "high"),
            ("prime", "high"): ("gemini-3.1-pro", "high"),
            ("core", "low"): ("gemini-3.6-flash", "low"),
            ("core", "medium"): ("gemini-3.6-flash", "medium"),
            ("core", "high"): ("gemini-3.6-flash", "high"),
            ("lite", "low"): ("gemini-3.5-flash", "low"),
            ("lite", "medium"): ("gemini-3.5-flash", "medium"),
            ("lite", "high"): ("gemini-3.5-flash", "medium"),
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
        self.assertEqual(settings.resolve_effort("low", "prime"), "low")
        self.assertEqual(settings.resolve_effort(None, "prime"), "high")

    def test_translation_order_is_tier_then_flat_then_identity(self):
        cfg = make_cfg(antigravity_efforts={"high": "flat-high"},
                       antigravity_tier_efforts={"prime": {"high": "tier-high"}})
        settings = cfg.adapter_settings(NAME)
        self.assertEqual(settings.resolve_requested_effort("prime", "high"),
                         "tier-high")
        self.assertEqual(settings.resolve_requested_effort("core", "high"),
                         "flat-high")
        self.assertEqual(settings.resolve_requested_effort("core", "low"), "low")

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
            antigravity_tier_efforts={"prime": {"low": "low"}})
        errors = AntigravityAdapter(conflicting, catalog=catalog()).preflight(
            [request("t001", "prime", "low", cfg=conflicting)])
        self.assertEqual(len(errors), 1)
        self.assertIn("conflicts with --effort=low", errors[0])
        self.assertIn("[adapter.antigravity.models] prime", errors[0])


class TestPreflight(unittest.TestCase):
    def test_shipped_mapping_passes_for_every_tier(self):
        adapter = make_adapter()
        requests = [request(f"t00{i}", tier, effort)
                    for i, (tier, effort) in enumerate(
                        [(t, e) for t in ("prime", "core", "lite")
                         for e in ("low", "medium", "high")], start=1)]
        self.assertEqual(adapter.preflight(requests), [])

    def test_wrong_pro_medium_mapping_names_the_exact_owner_and_fix(self):
        cfg = make_cfg(antigravity_tier_efforts={"prime": {"medium": "medium"}})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t007", "prime", "medium", cfg=cfg)])

        self.assertEqual(len(errors), 1)
        message = errors[0]
        self.assertIn("t007", message)
        self.assertIn("--model gemini-3.1-pro --effort medium", message)
        self.assertIn("available: low, high", message)
        self.assertIn("[adapter.antigravity.efforts.prime] medium = \"high\"", message)
        self.assertIn("current value 'medium'", message)

    def test_preflight_spends_no_subprocess_when_a_catalog_is_supplied(self):
        adapter = make_adapter()
        with mock.patch("assent.adapters.antigravity.subprocess.run",
                        side_effect=AssertionError("the CLI must not be started")):
            self.assertEqual(adapter.preflight([request()]), [])

    def test_unmapped_model_points_at_the_models_table(self):
        cfg = make_cfg(antigravity_models={"prime": "gemini-3.5-flash-lite"})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t001", "prime", "high", cfg=cfg)])
        self.assertEqual(len(errors), 1)
        self.assertIn("not in this installation's AGY catalog", errors[0])
        self.assertIn("[adapter.antigravity.models] prime", errors[0])

    def test_effortless_model_with_an_effort_is_refused(self):
        cfg = make_cfg(antigravity_models={"lite": "gemini-3-flash"})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([request("t002", "lite", "high", cfg=cfg)])
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
        cfg = make_cfg(antigravity_tier_efforts={"prime": {"medium": "medium"}})
        adapter = AntigravityAdapter(cfg, catalog=catalog())
        errors = adapter.preflight([
            request("t001", "prime", "medium", cfg=cfg),
            request("t001", "prime", "medium", cfg=cfg)])
        self.assertEqual(len(errors), 1)

    def test_recommendation_is_quality_first_then_the_family_ceiling(self):
        self.assertEqual(recommended_effort(("low", "high"), "medium"), "high")
        self.assertEqual(recommended_effort(("low", "medium"), "high"), "medium")
        self.assertEqual(recommended_effort(("low", "medium", "high"), "low"), "low")


class TestOutputContract(unittest.TestCase):
    def test_plain_text_is_shown_without_inventing_events(self):
        self.assertEqual(format_output_line("done\n"), "  AI| done")
        self.assertIn("!|", format_output_line("Error: something broke"))
        self.assertIsNone(format_output_line("   \n"))
        # a line that merely looks structured is still only a line of text
        rendered = format_output_line('{"type": "result", "usage": {"output_tokens": 5}}')
        self.assertTrue(rendered.startswith("  AI| "))
        self.assertIn('{"type": "result"', rendered)

    def test_failure_classification(self):
        cases = {
            "Error: invalid model selection (--model \"x\")": "unsupported_model",
            "Error: Resource has been exhausted (e.g. check quota).": "quota",
            "Error: quota exceeded for this project": "quota",
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

        def fake(command, cwd, stall_seconds, echo=None):
            captured.update(command=command, cwd=cwd, stall_seconds=stall_seconds)
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
        self.assertEqual(result.exit_code, 0)
        self.assertIsNone(result.failure_kind)
        self.assertFalse(result.quota_exhausted)

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


if __name__ == "__main__":
    unittest.main()
