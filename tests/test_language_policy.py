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

from assent.init import init as run_init


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
)


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


class LanguagePolicyTests(unittest.TestCase):
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
        documentation = [Path("AGENTS.md"), Path("README.md"), Path("README.zh-TW.md")]
        documentation.extend(
            path.relative_to(ROOT) for path in (ROOT / "docs").rglob("*.md")
        )
        documentation.extend(
            path.relative_to(ROOT)
            for path in (ROOT / "assent/templates").glob("*.md")
        )
        for path in documentation:
            with self.subTest(path=path):
                self.assertNotIn(".assent/agents.md", _read(path))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(
                ["git", "init"], cwd=root, check=True, capture_output=True
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run_init(root), 0)
            self.assertTrue((root / ".assent/instructions.md").is_file())
            self.assertFalse((root / ".assent/agents.md").exists())


if __name__ == "__main__":
    unittest.main()
