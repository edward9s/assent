"""preflight tests: the decisions that must be settled before an AI session exists.

Which abstract effort a task gets and which concrete CLI value that becomes, which adapter
resolves at all, and whether that adapter would accept every invocation the plan could still
issue. Both surfaces that consume those decisions are exercised, because `run` and `check`
must answer them identically and neither may spend a token to find out.  Both surfaces also
fail closed without current global contracts, so every case here mixes in
GlobalContractsMixin for a temporary user home.

Chinese literals that remain are deliberate user/upstream passthrough data."""
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from assent import engine, gitops, inspection, preflight
from assent.config import load_config
from assent.plan import journal_path_for, parse_task_file, set_status
from tests.engine_support import EngineTestCase, ScriptedAdapter, ok_result
from tests.test_contracts import GlobalContractsMixin


class TestInvocationResolution(GlobalContractsMixin, EngineTestCase):
    def test_resolved_effort_is_consistent_across_prompt_call_label_journal(self):
        # One resolved abstract/concrete pair must appear identically in the prompt
        # placeholders, the adapter call, the terminal label, and the scheduler journal.
        p1 = self.write_task(1, model="lite", effort="heavy")
        cfg = self.build(extra_config=
            '[adapter.claude.efforts.lite]\nheavy = "max"\n')
        self.commit_all()
        adapter = ScriptedAdapter([self.ai_done(p1)])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            engine.run(cfg, once=True, adapter=adapter)

        prompt, requested_model, requested_effort = adapter.calls[0]
        self.assertEqual(requested_model, "lite")
        self.assertEqual(requested_effort, "max")            # concrete CLI value
        self.assertIn('abstract effort = "heavy"', prompt)    # abstract kept distinct
        self.assertIn('requested_effort = "max"', prompt)
        # the prompt no longer offers "no value = the CLI default" as a session contract
        self.assertNotIn("CLI default", prompt)
        self.assertIn("| heavy->max", out.getvalue())

        from assent.plan import read_entries
        done = next(e for e in read_entries(journal_path_for(p1))
                    if e["by"] == "claude")
        self.assertEqual(done["requested_model"], "lite")

    def test_unknown_adapter_run_is_rejected_without_claude_fallback(self):
        self.write_task(1)
        cfg = self.build(adapter_name="nowhere")
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = engine.run(cfg)   # no injected adapter -> get_adapter must refuse
        self.assertEqual(rc, 1)
        self.assertIn("unknown adapter: 'nowhere'", out.getvalue())
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())

    def test_unknown_adapter_check_reports_fail_and_skips_cli_probe(self):
        self.write_task(1)
        cfg = self.build(adapter_name="nowhere")
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = inspection.check(cfg)
        self.assertEqual(rc, 1)
        self.assertIn("adapter: FAIL", out.getvalue())
        self.assertIn("unknown adapter: 'nowhere'", out.getvalue())
        # the CLI probe is adapter-provided, so an unresolved adapter emits no CLI line
        self.assertNotIn("CLI:", out.getvalue())
        self.assertNotIn("capability preflight", out.getvalue())

    def test_check_cli_probe_uses_current_adapter_command(self):
        # codex adapter with a runnable command (python) must be probed as codex, not claude
        self.write_task(1)
        cfg = self.build(adapter_name="codex", extra_config=
            '[adapter.codex]\ncommand = "python"\n')
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 0)
        self.assertIn("codex CLI: OK", out.getvalue())

    def test_check_cli_probe_reports_missing_executable(self):
        self.write_task(1)
        cfg = self.build(adapter_name="codex", extra_config=
            '[adapter.codex]\ncommand = "definitely-not-a-real-cli-xyz"\n')
        self.commit_all()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 1)
        self.assertIn("codex CLI: FAIL (executable not found", out.getvalue())

    def test_every_builtin_adapter_and_tier_resolves_a_concrete_effort(self):
        # No supported invocation may plan a None effort for a known tier.
        cfg = self.build()
        for tier in ("prime", "core", "lite"):
            for name in ("claude", "codex", "antigravity"):
                with self.subTest(adapter=name, tier=tier):
                    settings = cfg.adapter_settings(name)
                    effort = settings.resolve_effort(None, tier)
                    self.assertIsNotNone(effort)
                    self.assertIsNotNone(
                        settings.resolve_requested_effort(tier, effort))

    def test_effort_translation_uses_tier_then_flat_then_baseline(self):
        cfg = self.build(extra_config=
            '[adapter.claude.efforts]\n'
            'slight = "minimal"\nnormal = "balanced"\n'
            '[adapter.claude.efforts.lite]\nslight = "tiny"\n')
        self.assertEqual(preflight.resolve_requested_effort(cfg, "lite", "slight"),
                         "tiny")
        self.assertEqual(preflight.resolve_requested_effort(
            cfg, "lite", "normal"), "balanced")
        self.assertEqual(preflight.resolve_requested_effort(cfg, "lite", "heavy"),
                         "high")
        self.assertEqual(preflight.resolve_requested_effort(cfg, "core", "slight"),
                         "minimal")
        self.assertEqual(preflight.resolve_requested_effort(cfg, "core", "heavy"),
                         "high")

        tier_only = self.build(extra_config=
            '[adapter.claude.efforts.lite]\nheavy = "max"\n')
        self.assertEqual(preflight.resolve_requested_effort(
            tier_only, "lite", "heavy"), "max")
        self.assertEqual(preflight.resolve_requested_effort(
            tier_only, "lite", "slight"), "low")
        self.assertEqual(preflight.resolve_requested_effort(
            tier_only, "core", "heavy"), "high")


class TestAntigravityCapabilityPreflight(GlobalContractsMixin, EngineTestCase):
    """The active adapter proves every planned invocation before anything is spent.

    Antigravity is the adapter that actually publishes a capability catalog, so it is the one
    that exercises the shared gate; the adapters without one keep passing it trivially.
    """

    BAD_PRO_NORMAL = ('[adapter]\nname = "antigravity"\n'
                      '[adapter.antigravity.efforts.prime]\nnormal = "medium"\n')

    def setUp(self):
        super().setUp()
        from assent.adapters import antigravity
        self.catalog = antigravity.parse_models_catalog(
            (Path(__file__).resolve().parent / "fixtures"
             / "agy_models_1.1.5.txt").read_text(encoding="utf-8"))
        # Listing models costs nothing, but no test may reach a real installation.
        catalog_patch = mock.patch.object(
            antigravity, "load_catalog", return_value=self.catalog)
        catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        # Any attempt to open an actual AGY session is a test failure.
        session_patch = mock.patch.object(
            antigravity, "run_subprocess",
            side_effect=AssertionError("no AGY session may be started"))
        self.session = session_patch.start()
        self.addCleanup(session_patch.stop)

    def antigravity_cfg(self, extra_config=BAD_PRO_NORMAL):
        (self.root / ".assent" / "assent.toml").write_text(
            extra_config, encoding="utf-8")
        return load_config(self.root / ".assent" / "assent.toml", "plan01")

    def test_run_refuses_pro_normal_before_session_status_or_git_change(self):
        path = self.write_task(1, model="prime", effort="normal")
        cfg = self.antigravity_cfg()
        self.commit_all()
        commits_before = self._git("log", "--pretty=%H")

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(engine.run(cfg, once=True), 1)

        text = out.getvalue()
        self.assertIn("antigravity capability preflight: FAIL", text)
        self.assertIn("--model gemini-3.1-pro --effort medium", text)
        self.assertIn("available: low, high", text)
        self.assertIn('[adapter.antigravity.efforts.prime] normal = "high"', text)
        # nothing was started, marked, journalled or committed
        self.session.assert_not_called()
        self.assertEqual(parse_task_file(path).status, "TODO")
        self.assertFalse(journal_path_for(path).exists())
        self.assertEqual(self._git("log", "--pretty=%H"), commits_before)
        self.assertFalse(gitops.worktree_path(self.root, "plan01").exists())
        self.assertEqual(gitops.branches_with_prefix(self.root, "plan01/"), [])

    def test_check_refuses_the_same_mapping_with_the_same_diagnostic(self):
        self.write_task(1, model="prime", effort="normal")
        cfg = self.antigravity_cfg()
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(inspection.check(cfg), 1)

        text = out.getvalue()
        self.assertIn("antigravity capability preflight: FAIL", text)
        self.assertIn('[adapter.antigravity.efforts.prime] normal = "high"', text)
        self.session.assert_not_called()

    def test_shipped_mapping_passes_the_preflight_for_every_tier(self):
        for num, tier in enumerate(("prime", "core", "lite"), start=1):
            self.write_task(num, model=tier)
        cfg = self.antigravity_cfg('[adapter]\nname = "antigravity"\n')
        self.commit_all()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            inspection.check(cfg)
        self.assertIn("antigravity capability preflight: OK", out.getvalue())

    def test_settled_tasks_do_not_gate_a_run_they_cannot_join(self):
        self.write_task(1, model="prime", effort="normal", status="DONE")
        path = self.write_task(2, model="core")
        cfg = self.antigravity_cfg()
        self.commit_all()

        from assent.adapters.antigravity import AntigravityAdapter
        adapter = AntigravityAdapter(cfg, catalog=self.catalog)
        adapter.run_task = lambda prompt, model, effort, cwd: (
            set_status(path, "DONE") or ok_result())

        self.assertEqual(self.run_quiet(cfg, once=True, adapter=adapter), 0)
        self.assertEqual(parse_task_file(path).status, "DONE")


if __name__ == "__main__":
    unittest.main()
