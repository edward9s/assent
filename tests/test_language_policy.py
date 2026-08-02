"""Regression checks for the repository language boundary."""
from __future__ import annotations

import ast
import contextlib
import io
import os
import re
import subprocess
import tempfile
import tokenize
import unittest
from pathlib import Path
from unittest import mock

from assent.init import init as run_init
from assent.user_home import ASSENT_HOME_ENV


ROOT = Path(__file__).resolve().parents[1]
HAN_RE = re.compile(
    "[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
TRANSLATION_MARKERS = (
    "\u6b63\u9ad4\u4e2d\u6587",
    "\u82f1\u6587\u7248",
    "\u70ba\u6e96",
)
TRANSLATION_PAIRS = (
    (Path("README.md"), Path("README.zh-TW.md")),
    (Path("docs/CONSENSUS.md"), Path("docs/zh-TW/CONSENSUS.md")),
    (Path("docs/WORKFLOW.md"), Path("docs/zh-TW/WORKFLOW.md")),
    (Path("docs/COMMANDS.md"), Path("docs/zh-TW/COMMANDS.md")),
    (Path("docs/CONFIGURATION.md"), Path("docs/zh-TW/CONFIGURATION.md")),
    (Path("docs/VERIFICATION.md"), Path("docs/zh-TW/VERIFICATION.md")),
    (Path("docs/OPERATIONS.md"), Path("docs/zh-TW/OPERATIONS.md")),
)
# The two contracts live in the user home, so a project-relative spelling of either
# one is a stale claim in any language.  "~/.assent/format.md" ends in the same
# characters, so only an occurrence the user-home prefix does not introduce counts.
CONTRACT_PATH_TAILS = (".assent/instructions.md", ".assent/format.md")


def _read(relative: Path) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _assert_no_han(test: unittest.TestCase, relative: Path) -> None:
    text = _read(relative)
    match = HAN_RE.search(text)
    if match:
        line = text.count("\n", 0, match.start()) + 1
        test.fail(f"Han character in canonical English file {relative}:{line}")


def _case_sensitive_file(relative: Path) -> bool:
    current = ROOT
    for part in relative.parts:
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        if not current.is_dir():
            return False
        names = {child.name for child in current.iterdir()}
        if part not in names:
            return False
        current /= part
    return current.is_file()


def _markdown_targets(text: str) -> set[str]:
    return {
        match.group(1).split("#", 1)[0]
        for match in re.finditer(r"(?<!!)\[[^\]]+\]\(([^)]+)\)", text)
    }


def _project_local_contract_claims(text: str) -> list[str]:
    """Return every contract path written as a project path instead of a user-home one."""
    found = []
    for tail in CONTRACT_PATH_TAILS:
        start = 0
        while True:
            index = text.find(tail, start)
            if index < 0:
                break
            start = index + 1
            if text[max(0, index - 2):index] == "~/":
                continue
            line = text.count("\n", 0, index) + 1
            found.append(f"{line}: {tail}")
    return found


def _documentation_and_templates() -> list[Path]:
    """Every tracked page and packaged template that states where files live."""
    paths = [Path("AGENTS.md"), Path("README.md"), Path("README.zh-TW.md")]
    paths.extend(path.relative_to(ROOT) for path in (ROOT / "docs").rglob("*.md"))
    paths.extend(
        path.relative_to(ROOT) for path in (ROOT / "assent/templates").glob("*.md")
    )
    return paths


def _tracked_old_brand_matches() -> list[tuple[str, int, str]]:
    """Return tracked old-brand spellings, with path and one-based line number."""
    result = subprocess.run(
        ["git", "grep", "-n", "-I", "-i", "-E",
         r"(^|[^[:alnum:]_])agents([^[:alnum:]_]|$)"],
        cwd=ROOT, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode not in (0, 1):
        raise AssertionError(result.stderr)
    matches = []
    for record in result.stdout.splitlines():
        path, line, text = record.split(":", 2)
        matches.append((path, int(line), text))
    return matches


def _is_audited_old_brand_exception(path: str, text: str) -> bool:
    """Allow only verbatim history evidence, fixtures, and the AGENTS.md filename."""
    if path == "tests/fixtures/stream_json_ok.txt":
        return True  # Verbatim external-protocol fixture.
    if "AGENTS" in text and ".agents" not in text:
        return True  # The standard agent-tool instruction filename.
    if path == "tests/test_language_policy.py":
        return True  # This audit's patterns and its narrow exception definitions.
    return False


class LanguagePolicyTests(unittest.TestCase):
    def test_tracked_old_brand_audit_has_only_narrow_exceptions(self):
        """Current product text must not regain the former package or path names."""
        unexpected = [
            f"{path}:{line}: {text}"
            for path, line, text in _tracked_old_brand_matches()
            if not _is_audited_old_brand_exception(path, text)
        ]
        self.assertEqual(unexpected, [])

    def test_package_metadata_and_templates_use_only_assent(self):
        self.assertTrue((ROOT / "assent").is_dir())
        self.assertFalse((ROOT / "agents").exists())
        self.assertIn('name = "assent"', _read(Path("pyproject.toml")))
        self.assertIn('assent = "assent.__main__:main"', _read(Path("pyproject.toml")))
        for path in (ROOT / "assent/templates").iterdir():
            if path.is_file() and path.name != "AGENTS.md":
                with self.subTest(path=path):
                    self.assertNotIn(".agents", path.read_text(encoding="utf-8"))

    def test_canonical_documents_contain_no_han_characters(self):
        paths = [Path("AGENTS.md"), Path("README.md")]
        paths.extend(
            path.relative_to(ROOT)
            for path in (ROOT / "docs").rglob("*.md")
            if "zh-TW" not in path.relative_to(ROOT / "docs").parts
        )
        paths.extend(
            path.relative_to(ROOT)
            for path in (ROOT / "assent/templates").glob("*.md")
        )
        for path in paths:
            with self.subTest(path=path):
                _assert_no_han(self, path)

    def test_runtime_product_files_contain_no_han_characters(self):
        paths = [
            path.relative_to(ROOT)
            for path in (ROOT / "assent").rglob("*.py")
        ]
        paths.extend([
            Path("assent/templates/assent.toml"),
            Path("pyproject.toml"),
            Path(".gitignore"),
        ])
        for path in paths:
            with self.subTest(path=path):
                _assert_no_han(self, path)

    def test_test_comments_and_docstrings_contain_no_han_characters(self):
        for path in (ROOT / "tests").rglob("*.py"):
            relative = path.relative_to(ROOT)
            source = path.read_text(encoding="utf-8")
            with self.subTest(path=relative, kind="comment"):
                tokens = tokenize.generate_tokens(io.StringIO(source).readline)
                for token in tokens:
                    if token.type == tokenize.COMMENT and HAN_RE.search(token.string):
                        self.fail(
                            f"Han character in test comment {relative}:{token.start[0]}"
                        )
            tree = ast.parse(source, filename=str(relative))
            for node in ast.walk(tree):
                if not isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ):
                    continue
                if not node.body:
                    continue
                first = node.body[0]
                if (
                    isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)
                    and HAN_RE.search(first.value.value)
                ):
                    self.fail(
                        f"Han character in test docstring {relative}:{first.lineno}"
                    )

    def test_required_translations_and_reciprocal_links(self):
        for canonical, translation in TRANSLATION_PAIRS:
            with self.subTest(translation=translation):
                self.assertTrue(_case_sensitive_file(canonical), canonical)
                self.assertTrue(_case_sensitive_file(translation), translation)
                translated_text = _read(translation)
                self.assertRegex(translated_text, HAN_RE)
                for marker in TRANSLATION_MARKERS:
                    self.assertIn(marker, translated_text)
                self.assertIn("English", translated_text)

                canonical_target = translation.relative_to(canonical.parent).as_posix()
                translation_target = Path(
                    os.path.relpath(canonical, translation.parent)
                ).as_posix()
                self.assertIn(canonical_target, _markdown_targets(_read(canonical)))
                self.assertIn(translation_target, _markdown_targets(translated_text))
                self.assertTrue(
                    _case_sensitive_file(canonical.parent / canonical_target)
                )
                self.assertTrue(
                    _case_sensitive_file(translation.parent / translation_target)
                )

    def test_auto_fix_language_boundary_matches_the_reader_split(self):
        english_paths = [
            Path("AGENTS.md"), Path("README.md"),
            Path("assent/templates/assent.toml"),
            Path("assent/templates/instructions.md"),
            Path("assent/templates/format.md"),
            Path("docs/WORKFLOW.md"), Path("docs/COMMANDS.md"),
            Path("docs/CONFIGURATION.md"), Path("docs/VERIFICATION.md"),
            Path("docs/OPERATIONS.md"), Path("docs/CONSENSUS.md"),
        ]
        for path in english_paths:
            with self.subTest(language="English", path=path):
                text = _read(path)
                self.assertNotRegex(text, HAN_RE)
                self.assertIn("run --auto-fix", text)

        chinese_paths = [
            Path("README.zh-TW.md"),
            Path("docs/zh-TW/WORKFLOW.md"),
            Path("docs/zh-TW/COMMANDS.md"),
            Path("docs/zh-TW/CONFIGURATION.md"),
            Path("docs/zh-TW/VERIFICATION.md"),
            Path("docs/zh-TW/OPERATIONS.md"),
            Path("docs/zh-TW/CONSENSUS.md"),
        ]
        for path in chinese_paths:
            with self.subTest(language="Traditional Chinese", path=path):
                text = _read(path)
                self.assertRegex(text, HAN_RE)
                self.assertIn("run --auto-fix", text)
                self.assertIn("English", text)

    def test_session_rules_have_one_packaged_name_and_fresh_init_path(self):
        self.assertTrue((ROOT / "assent/templates/instructions.md").is_file())
        instruction_templates = [
            path.relative_to(ROOT)
            for path in (ROOT / "assent/templates").rglob("instructions.md")
        ]
        self.assertEqual(
            instruction_templates, [Path("assent/templates/instructions.md")]
        )
        template_names = {
            path.name for path in (ROOT / "assent/templates").iterdir()
        }
        self.assertIn("AGENTS.md", template_names)
        self.assertNotIn("agents.md", template_names)
        for path in _documentation_and_templates():
            with self.subTest(path=path):
                self.assertNotIn(".assent/agents.md", _read(path))

        # The session rules a fresh init installs land in the user home, so this
        # redirects ASSENT_HOME and never reads or writes the operator's own one.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            home = Path(directory) / "home"
            root.mkdir()
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True
            )
            with mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(home)}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_init(root), 0)
            self.assertTrue((home / "instructions.md").is_file())
            self.assertFalse((home / "agents.md").exists())
            self.assertFalse((root / ".assent/instructions.md").exists())
            self.assertFalse((root / ".assent/agents.md").exists())

    def test_documentation_states_the_user_home_contract_paths(self):
        """Every page must name the contracts where they actually are."""
        stale = [
            f"{path}:{claim}"
            for path in _documentation_and_templates()
            for claim in _project_local_contract_claims(_read(path))
        ]
        self.assertEqual(stale, [])

    def test_antigravity_surfaces_do_not_prescribe_removed_auth_subcommand(self):
        """AGY sign-in documentation must start the interactive CLI."""
        surfaces = (
            Path("README.md"),
            Path("README.zh-TW.md"),
            Path("docs/CONFIGURATION.md"),
            Path("docs/zh-TW/CONFIGURATION.md"),
            Path("assent/templates/assent.toml"),
        )
        for path in surfaces:
            with self.subTest(path=path):
                self.assertNotIn("agy auth login", _read(path))

    def test_fresh_init_creates_no_project_copy_of_the_shared_files(self):
        """The three shared files belong to the user home, in fact and in the docs."""
        # As above, ASSENT_HOME is redirected so the operator's own home is untouched.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "project"
            home = Path(directory) / "home"
            root.mkdir()
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True
            )
            with mock.patch.dict(os.environ, {ASSENT_HOME_ENV: str(home)}):
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(run_init(root), 0)
            for name in ("assent.toml", "instructions.md", "format.md"):
                with self.subTest(name=name):
                    self.assertTrue((home / name).is_file())
                    self.assertFalse((root / ".assent" / name).exists())
            self.assertTrue((root / ".assent/verify.py").is_file())


if __name__ == "__main__":
    unittest.main()
