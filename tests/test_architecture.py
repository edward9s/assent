"""Regression checks for module boundaries inside the ``assent`` package.

A leading underscore marks a symbol as belonging to the module that defines it.
When one production module imports another's underscored name, that private
symbol has quietly become a shared contract without a stated owner, and the two
modules can no longer be changed independently.  This test reads the production
sources with ``ast`` -- no imports, so it also holds for modules a given test run
would never load -- and fails on any such cross-module reach.

Module-local private helpers stay private on purpose; only crossing a module
boundary is refused.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "assent"


def _production_files() -> list[Path]:
    """Every shipped ``assent`` source file, excluding packaged templates."""
    templates = PACKAGE / "templates"
    return sorted(path for path in PACKAGE.rglob("*.py")
                  if templates not in path.parents)


def _is_private(name: str) -> bool:
    """True for a module-private name; dunders are language protocol, not that."""
    return name.startswith("_") and not (
        name.startswith("__") and name.endswith("__"))


def _imported_assent_modules(name: str) -> set[str]:
    """Every ``assent`` submodule the named module imports, by bare name."""
    tree = ast.parse((PACKAGE / f"{name}.py").read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                continue
            if module == "assent":
                imported.update(alias.name for alias in node.names)
            elif module.startswith("assent."):
                imported.add(module.split(".", 1)[1])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("assent."):
                    imported.add(alias.name.split(".", 1)[1])
    return imported


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local names bound to another ``assent`` module by this file's imports."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "assent" or alias.name.startswith("assent."):
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level or not (module == "assent"
                                  or module.startswith("assent.")):
                continue
            for alias in node.names:
                bound = f"{module}.{alias.name}"
                if (PACKAGE / Path(*bound.split(".")[1:])).with_suffix(
                        ".py").is_file():
                    aliases[alias.asname or alias.name] = bound
    return aliases


class PrivateCrossModuleImports(unittest.TestCase):
    """No production module may reach into another module's private names."""

    def test_no_private_symbol_is_imported_from_another_module(self) -> None:
        offenders: list[str] = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                module = node.module or ""
                if node.level or not (module == "assent"
                                      or module.startswith("assent.")):
                    continue
                for alias in node.names:
                    if _is_private(alias.name):
                        offenders.append(
                            f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: "
                            f"from {module} import {alias.name}")
        self.assertEqual(offenders, [], "\n".join(
            ["private symbols imported across assent module boundaries:",
             *offenders]))

    def test_no_private_attribute_is_read_from_another_module(self) -> None:
        offenders: list[str] = []
        for path in _production_files():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            aliases = _module_aliases(tree)
            if not aliases:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                value = node.value
                if not isinstance(value, ast.Name) or value.id not in aliases:
                    continue
                if _is_private(node.attr):
                    offenders.append(
                        f"{path.relative_to(ROOT).as_posix()}:{node.lineno}: "
                        f"{aliases[value.id]}.{node.attr}")
        self.assertEqual(offenders, [], "\n".join(
            ["private attributes read across assent module boundaries:",
             *offenders]))

    def test_the_check_would_catch_a_reintroduced_private_import(self) -> None:
        """The scan is only worth anything if it actually rejects an offender."""
        tree = ast.parse("from assent.accept import _source_snapshot\n")
        node = next(n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom))
        self.assertTrue(_is_private(node.names[0].name))
        self.assertFalse(_is_private("resolve_source_snapshot"))
        self.assertFalse(_is_private("__all__"))


class NeutralFolderServices(unittest.TestCase):
    """The shared folder services live in neutral modules, not in a command."""

    def test_shared_helpers_are_public_in_their_owning_modules(self) -> None:
        from assent import clean, config, folder_source, folderdeps
        self.assertEqual(folder_source.COMPLETE_STATUSES, ("DONE", "SKIP"))
        for owner, name in (
                (folder_source, "resolve_source_snapshot"),
                (folderdeps, "direct_dependents"),
                (config, "validate_tasks_name"),
                (clean, "clean_locked"),
                (clean, "has_cleanup_target")):
            with self.subTest(symbol=f"{owner.__name__}.{name}"):
                self.assertTrue(callable(getattr(owner, name)))

    def test_the_neutral_source_module_depends_on_no_command_module(self) -> None:
        """``folder_source`` must not import back into accept/clean/reject/....

        A neutral service that imported a command would only move the coupling,
        and would put an import cycle between the two.
        """
        tree = ast.parse((PACKAGE / "folder_source.py").read_text(
            encoding="utf-8"))
        commands = {"accept", "archive", "clean", "reconcile", "reject",
                    "rework", "verification", "engine"}
        imported = {
            alias.name
            for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            and (node.module or "") == "assent"
            for alias in node.names
        }
        self.assertEqual(imported & commands, set())


class VerificationModuleBoundaries(unittest.TestCase):
    """Verification is a facade over receipt leaves and one closeout boundary.

    ``assent.verification`` re-exports; ``folder_verification`` and
    ``batch_receipt`` own one receipt model each; ``batch_verification`` runs the
    batch and writes the evidence ``batch_receipt`` defines; and
    ``folder_verification_closeout`` owns the shared per-folder report handoff.
    ``verification_common`` sits below all receipt implementations without
    knowing any of them.
    """

    LEAVES = ("folder_verification", "batch_receipt", "batch_verification",
              "folder_verification_closeout")

    def test_the_facade_defines_no_implementation_of_its_own(self) -> None:
        tree = ast.parse((PACKAGE / "verification.py").read_text(
            encoding="utf-8"))
        defined = [node.name for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.ClassDef))]
        self.assertEqual(defined, [])

    def test_the_common_base_knows_none_of_the_modules_above_it(self) -> None:
        imported = _imported_assent_modules("verification_common")
        self.assertEqual(imported & {*self.LEAVES, "verification"}, set())

    def test_the_leaves_form_no_import_cycle(self) -> None:
        # Batch execution may use the batch receipt it writes, and closeout may
        # use the folder receipt it wraps; no other edge between these modules
        # is allowed, and none of them imports the facade.
        allowed = {
            "folder_verification": set(),
            "batch_receipt": set(),
            "batch_verification": {"batch_receipt"},
            "folder_verification_closeout": {"folder_verification"},
        }
        for leaf in self.LEAVES:
            with self.subTest(module=leaf):
                imported = _imported_assent_modules(leaf)
                self.assertNotIn("verification", imported)
                self.assertEqual(imported & set(self.LEAVES), allowed[leaf])

    def test_closeout_uses_static_receipt_and_report_edges(self) -> None:
        imported = _imported_assent_modules("folder_verification_closeout")
        self.assertEqual(imported & {"folder_verification", "inspection"},
                         {"folder_verification", "inspection"})

    def test_inspection_reads_folder_receipt_lines_from_their_owner(self) -> None:
        imported = _imported_assent_modules("inspection")
        self.assertIn("folder_verification", imported)
        self.assertNotIn("verification", imported)

    def test_folder_closeout_edge_is_not_hidden_by_runtime_lookup(self) -> None:
        for module in ("folder_verification", "folder_verification_closeout"):
            with self.subTest(module=module):
                source = (PACKAGE / f"{module}.py").read_text(encoding="utf-8")
                self.assertNotIn("import_module", source)
                self.assertNotIn("__getattr__", source)

    def test_every_verification_entry_point_stays_importable(self) -> None:
        from assent import verification
        for name in ("verify_folder", "verify_folder_if_needed", "verify_batch",
                     "verify_selected_batch", "receipt_matches_current_candidate",
                     "receipt_report_lines", "invalidate_folder_receipt",
                     "invalidate_batch_receipt", "read_receipt", "write_receipt",
                     "read_batch_receipt", "write_batch_receipt",
                     "batch_receipt_staleness", "build_batch_candidate",
                     "verifier_digest"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(verification, name)))
        for name in ("RECEIPT_NAME", "BATCH_RECEIPT_NAME", "VERIFY_COMMAND",
                     "VerificationReceipt", "BatchVerificationReceipt",
                     "BatchSource"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(verification, name))


class AcceptanceModuleBoundaries(unittest.TestCase):
    """Direct acceptance and batch acceptance are two modules, one direction.

    ``assent.accept`` owns the single receipt-backed transaction;
    ``assent.batch_accept`` owns the selected batch, the batch release, and
    ``accept --all``, and reuses the direct transaction through its public
    helpers.  Reading or changing either safety-sensitive path must never
    require loading the other implementation, so the edge runs one way only.
    """

    def test_the_direct_module_does_not_import_the_batch_module(self) -> None:
        self.assertNotIn("batch_accept", _imported_assent_modules("accept"))

    def test_the_batch_module_reuses_the_direct_transaction(self) -> None:
        self.assertIn("accept", _imported_assent_modules("batch_accept"))

    def test_each_module_defines_only_its_own_entry_points(self) -> None:
        from assent import accept, batch_accept
        for owner, name in ((accept, "accept_folder"),
                            (accept, "accept_merge_message"),
                            (accept, "cleanup_warning"),
                            (accept, "dependency_tip"),
                            (batch_accept, "accept_all"),
                            (batch_accept, "accept_selected_batch")):
            with self.subTest(symbol=f"{owner.__name__}.{name}"):
                function = getattr(owner, name)
                self.assertTrue(callable(function))
                self.assertEqual(function.__module__, owner.__name__)
        for absent in ("accept_all", "accept_selected_batch"):
            with self.subTest(absent=absent):
                self.assertFalse(hasattr(accept, absent))

    def test_the_cli_takes_each_path_from_its_owning_module(self) -> None:
        """The command syntax is unchanged, so the dispatch targets must be too."""
        from assent import __main__
        self.assertEqual(__main__.accept_folder.__module__, "assent.accept")
        for name in ("accept_all", "accept_selected_batch"):
            with self.subTest(name=name):
                self.assertEqual(getattr(__main__, name).__module__,
                                 "assent.batch_accept")


class ExecutionAndInspectionBoundaries(unittest.TestCase):
    """Unattended execution, read-only inspection, and their shared preflight.

    ``assent.engine`` runs sessions; ``assent.inspection`` owns report/status/
    check; ``assent.preflight`` owns the decisions both must answer identically
    (effort, adapter capability, assignment rendering, git layering, stack
    state).  The edges run one way -- engine -> inspection -> preflight -- so a
    query command never has to load the scheduler, and preflight never has to
    load either of its callers.
    """

    def test_neither_new_module_imports_the_engine(self) -> None:
        for module in ("inspection", "preflight"):
            with self.subTest(module=module):
                self.assertNotIn("engine", _imported_assent_modules(module))

    def test_preflight_does_not_import_inspection(self) -> None:
        self.assertNotIn("inspection", _imported_assent_modules("preflight"))

    def test_the_engine_takes_report_and_preflight_from_their_owners(self) -> None:
        imported = _imported_assent_modules("engine")
        self.assertIn("inspection", imported)
        self.assertIn("preflight", imported)

    def test_each_module_defines_only_its_own_entry_points(self) -> None:
        from assent import engine, inspection, preflight
        for owner, name in ((engine, "run"),
                            (engine, "verify_focused"),
                            (inspection, "report"),
                            (inspection, "status"),
                            (inspection, "check"),
                            (inspection, "render_report"),
                            (inspection, "write_report"),
                            (inspection, "try_write_report"),
                            (preflight, "resolve_session"),
                            (preflight, "capability_errors"),
                            (preflight, "resolve_stack_state")):
            with self.subTest(symbol=f"{owner.__name__}.{name}"):
                function = getattr(owner, name)
                self.assertTrue(callable(function))
                self.assertEqual(function.__module__, owner.__name__)
        for absent in ("report", "status", "check", "render_report"):
            with self.subTest(absent=absent):
                self.assertFalse(hasattr(engine, absent))

    def test_the_cli_and_rework_take_reporting_from_inspection(self) -> None:
        """The command syntax is unchanged, so the dispatch targets must be too."""
        from assent import __main__, rework
        for name in ("report", "status", "check"):
            with self.subTest(name=name):
                self.assertEqual(
                    getattr(__main__.inspection, name).__module__,
                    "assent.inspection")
        self.assertEqual(rework.write_report.__module__, "assent.inspection")
        self.assertNotIn("engine", _imported_assent_modules("rework"))


class VendorAdapterIndependence(unittest.TestCase):
    """One vendor's adapter never depends on another vendor's adapter module.

    Shared execution machinery belongs to a neutral module such as
    ``assent.adapters.process``; reaching into a sibling vendor module instead
    makes that vendor's file impossible to change without breaking the others.
    """

    ADAPTERS = PACKAGE / "adapters"
    NEUTRAL = {"__init__", "process"}

    def _vendor_modules(self) -> list[Path]:
        return sorted(path for path in self.ADAPTERS.glob("*.py")
                      if path.stem not in self.NEUTRAL)

    def test_the_vendor_modules_under_check_are_the_expected_ones(self) -> None:
        self.assertEqual([path.stem for path in self._vendor_modules()],
                         ["antigravity", "claude", "codex"])

    def test_no_vendor_adapter_imports_another_vendor_adapter(self) -> None:
        vendors = {path.stem for path in self._vendor_modules()}
        offenders: list[str] = []
        for path in self._vendor_modules():
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    names = [f"{node.module or ''}.{alias.name}"
                             for alias in node.names]
                elif isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                else:
                    continue
                for name in names:
                    parts = name.split(".")
                    if parts[:2] != ["assent", "adapters"] or len(parts) < 3:
                        continue
                    if parts[2] in vendors and parts[2] != path.stem:
                        offenders.append(
                            f"{path.relative_to(ROOT).as_posix()}:"
                            f"{node.lineno}: {name}")
        self.assertEqual(offenders, [], "\n".join(
            ["vendor adapters importing another vendor adapter:", *offenders]))

    def test_the_shared_runner_lives_in_the_neutral_process_module(self) -> None:
        from assent.adapters import process
        self.assertTrue(callable(process.run_subprocess))


class AutoFixStateSchema(unittest.TestCase):
    """The version-4 state validator and dataclass expose one exact schema."""

    def test_version_and_exact_field_set_stay_in_parity(self) -> None:
        from dataclasses import fields
        from assent import auto_fix

        self.assertEqual(auto_fix.AUTO_FIX_STATE_VERSION, 4)
        self.assertEqual(
            {field.name for field in fields(auto_fix.AutoFixState)},
            auto_fix._STATE_KEYS)
        self.assertIn("phase", auto_fix._STATE_KEYS)
        for field in (
                "review_context", "review_stage", "failure_trigger",
                "reviewer_recommendations", "approved_scope_additions",
                "scope_amendments", "worker_dispositions", "repair_briefs",
                "plan_digest_transitions", "review_transitions"):
            self.assertIn(field, auto_fix._STATE_KEYS)
        self.assertEqual(auto_fix.AUTO_FIX_PHASES, frozenset({
            "NEEDS_REPAIR", "REPAIRING", "AWAITING_REVIEW", "COMPLETE",
        }))

    def test_review_enums_are_bounded_and_exclude_verification_receipts(self) -> None:
        from assent import auto_fix

        self.assertEqual(auto_fix.REVIEW_CONTEXTS,
                         {"completed_folder", "blocked_adjudication"})
        self.assertEqual(auto_fix.REVIEW_STAGES, {"initial", "recheck"})
        self.assertNotIn("complete_verification", auto_fix.REVIEW_FINDING_KINDS)
        self.assertNotIn("receipt_absence", auto_fix.REVIEW_FINDING_KINDS)


if __name__ == "__main__":
    unittest.main()
