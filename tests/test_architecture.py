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


if __name__ == "__main__":
    unittest.main()
