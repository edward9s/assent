"""Regression checks for the split reader documentation."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
TOPICS = (
    "WORKFLOW",
    "COMMANDS",
    "CONFIGURATION",
    "VERIFICATION",
    "OPERATIONS",
)
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")
CHINESE_PLANNING_PROMPT = (
    "請簡潔回答，不要用子代理。如果你看到源碼有任何bug、壞結構，或說明文件與程式行為不符合，就回報我。"
    "以下是本專案需要討論的問題，不要過度設計，先徵得人類的同意，依照 assent 格式產生相關的計畫書：\n"
    "1. 需求描述。\n"
    "2. 需求描述。\n"
    "3. 需求描述。"
)


def _read(relative: Path) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _maintained_surfaces() -> list[Path]:
    paths = [Path("README.md"), Path("README.zh-TW.md")]
    paths.extend(path.relative_to(ROOT) for path in (ROOT / "docs").rglob("*.md"))
    return paths


def _relative_markdown_targets(text: str) -> list[str]:
    targets = []
    for match in LINK_RE.finditer(text):
        target = unquote(match.group(1)).split("#", 1)[0]
        if (
            not target
            or target.startswith(("#", "<", "~", "/"))
            or "://" in target
            or target.startswith("mailto:")
        ):
            continue
        targets.append(target)
    return targets


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


class DocumentationTests(unittest.TestCase):
    def test_readmes_stay_within_onboarding_line_budget(self):
        for relative in (Path("README.md"), Path("README.zh-TW.md")):
            with self.subTest(path=relative):
                self.assertLessEqual(len(_read(relative).splitlines()), 450)

    def test_all_five_guides_have_english_and_translation_files(self):
        for topic in TOPICS:
            english = Path("docs") / f"{topic}.md"
            translated = Path("docs/zh-TW") / f"{topic}.md"
            with self.subTest(topic=topic):
                self.assertTrue((ROOT / english).is_file())
                self.assertTrue((ROOT / translated).is_file())
                self.assertIn("../README.md", _read(english))
                self.assertIn("zh-TW/" + f"{topic}.md", _read(english))
                self.assertIn("../../README.zh-TW.md", _read(translated))
                self.assertIn("../" + f"{topic}.md", _read(translated))

    def test_maintained_markdown_file_links_resolve_case_sensitively(self):
        for relative in _maintained_surfaces():
            source = ROOT / relative
            for target in _relative_markdown_targets(_read(relative)):
                resolved = (source.parent / target).resolve()
                try:
                    relative_target = resolved.relative_to(ROOT)
                except ValueError:
                    relative_target = Path("__outside_repository__")
                with self.subTest(source=relative, target=target):
                    self.assertTrue(
                        _case_sensitive_file(relative_target),
                        f"broken link: {target}",
                    )

    def test_readmes_show_pypi_install_uninstall_and_retention_boundary(self):
        required_commands = (
            "python -m pip install assent",
            "python -m pip uninstall assent",
        )
        required_terms = (
            "~/.assent",
            ".assent/",
            "worktree",
            "archive",
            "git branch",
        )
        for relative in (Path("README.md"), Path("README.zh-TW.md")):
            text = _read(relative).lower()
            with self.subTest(path=relative):
                for command in required_commands:
                    self.assertIn(command, text)
                for term in required_terms:
                    self.assertIn(term.lower(), text)
                self.assertRegex(text, r"(?:does not delete|不會刪除)")

    def test_prompts_keep_planning_and_independent_review_safeguards(self):
        english = _read(Path("README.md")) + _read(Path("docs/WORKFLOW.md"))
        for phrase in (
            "Answer concisely",
            "do not use subagents",
            "source bug",
            "bad structure",
            "documentation/runtime mismatch",
            "Do not overengineer",
            "explicit human agreement",
            "Assent-format task files",
            "Requirement description",
            "_report.md",
            "task and journal files",
            "checkpoint commit and diff",
            "focused and full verification evidence",
            "evidence-based findings first",
            "Never accept or rework automatically",
            "Wait for the human decision",
            "different vendor",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, english)
        self.assertIn(CHINESE_PLANNING_PROMPT, _read(Path("README.zh-TW.md")))
        chinese_review = _read(Path("README.zh-TW.md")) + _read(
            Path("docs/zh-TW/WORKFLOW.md")
        )
        for phrase in ("不要用子代理", "絕不要自動 accept 或 rework", "等待人類決定"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, chinese_review)


if __name__ == "__main__":
    unittest.main()
