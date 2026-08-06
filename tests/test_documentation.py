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

    def test_chinese_readme_topic_columns_point_to_the_named_languages(self):
        text = _read(Path("README.zh-TW.md"))
        for topic in TOPICS:
            with self.subTest(topic=topic):
                row = next(
                    line for line in text.splitlines()
                    if line.startswith("|") and f"[{topic}]" in line
                )
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                self.assertEqual(len(cells), 3)
                self.assertIn(f"(docs/{topic}.md)", cells[1])
                self.assertIn(f"(docs/zh-TW/{topic}.md)", cells[2])

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
            "This ordinary acceptance review remains human-driven",
            "explicit `run --auto-fix`",
            "still never accepts a folder",
            "Wait for the human decision",
            "different vendor",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, english)
        self.assertIn(CHINESE_PLANNING_PROMPT, _read(Path("README.zh-TW.md")))
        chinese_review = _read(Path("README.zh-TW.md")) + _read(
            Path("docs/zh-TW/WORKFLOW.md")
        )
        for phrase in (
                "不要用子代理",
                "這個一般驗收審查由人類主導",
                "`run --auto-fix`",
                "絕不自動接受 folder",
                "等待人類決定"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, chinese_review)

    def test_auto_fix_is_explicit_bounded_and_never_acceptance(self):
        english = "\n".join(
            _read(path) for path in (Path("README.md"), Path("docs/WORKFLOW.md"),
                                     Path("docs/COMMANDS.md"),
                                     Path("docs/VERIFICATION.md")))
        for phrase in (
                "`run --auto-fix`",
                "selection-orthogonal",
            "SELF-FIXED, UNREVIEWED",
            "never creates tasks",
            "never accepts",
            "pre-existing technical debt",
            "directly interacting code",
            "repository-wide debt audit",
                "_auto_fix.toml",
                "read-only"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(phrase, english)
        self.assertNotIn("Never accept or rework automatically", english)

        chinese = "\n".join(
            _read(path) for path in (Path("README.zh-TW.md"),
                                     Path("docs/zh-TW/WORKFLOW.md"),
                                     Path("docs/zh-TW/COMMANDS.md"),
                                     Path("docs/zh-TW/VERIFICATION.md")))
        for phrase in (
                "`run --auto-fix`",
                "與選取正交",
                "SELF-FIXED, UNREVIEWED",
            "不會自動建立 task",
            "絕不自動接受 folder",
                "既有 technical debt",
                "直接互動程式碼",
                "全 repository debt audit",
                "`_auto_fix.toml`",
                "唯讀"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_auto_fix_round_budget_and_recovery_identity_stay_in_parity(self):
        """Reader surfaces must describe round-scoped budgets and fail-closed recovery."""
        english_paths = [
            Path("AGENTS.md"), Path("README.md"),
            Path("assent/templates/assent.toml"),
            Path("assent/templates/instructions.md"),
            Path("assent/templates/format.md"),
            Path("docs/WORKFLOW.md"), Path("docs/COMMANDS.md"),
            Path("docs/CONFIGURATION.md"), Path("docs/VERIFICATION.md"),
            Path("docs/OPERATIONS.md"), Path("docs/CONSENSUS.md"),
        ]
        english = "\n".join(_read(path) for path in english_paths)
        for phrase in (
                "repair round", "multi-task", "dependency cascade",
                "first write-capable session", "refuses repair and closeout",
                "resolved reviewer identity", "phase"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(phrase, english)
        self.assertNotIn("before each write-capable session", english)

        chinese_paths = [
            Path("README.zh-TW.md"),
            Path("docs/zh-TW/WORKFLOW.md"),
            Path("docs/zh-TW/COMMANDS.md"),
            Path("docs/zh-TW/CONFIGURATION.md"),
            Path("docs/zh-TW/VERIFICATION.md"),
            Path("docs/zh-TW/OPERATIONS.md"),
            Path("docs/zh-TW/CONSENSUS.md"),
        ]
        chinese = "\n".join(_read(path) for path in chinese_paths)
        for phrase in (
                "repair round", "多 task", "dependency cascade",
                "第一個 write-capable session", "拒絕 repair 與 closeout",
                "resolved reviewer identity", "Version 5"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(phrase, chinese)
        self.assertNotIn("每個 write-capable session 前", chinese)

    def test_review_unresolved_outcome_and_settling_gate_stay_in_parity(self):
        """The gated settle, its failing-gate outcome, and REVIEW UNRESOLVED,
        HUMAN DECISION must reach every reader doc surface in both languages,
        distinct from SELF-FIXED, UNREVIEWED and from BLOCKED.
        """
        english_paths = [
            Path("docs/WORKFLOW.md"), Path("docs/COMMANDS.md"),
            Path("docs/VERIFICATION.md"),
        ]
        english = "\n".join(_read(path) for path in english_paths)
        for phrase in (
                "REVIEW UNRESOLVED, HUMAN DECISION",
                "settling gate",
                "distinct outcome",
                "exits zero",
                "queued behind it"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(phrase, english)
        for path in english_paths:
            text = _read(path)
            with self.subTest(document=str(path)):
                self.assertNotIn(
                    "an unrepaired blocker preserves every finding, edit, "
                    "and journal without another round and exits\nnonzero",
                    text)
                self.assertNotIn(
                    "unrepaired blocker preserves every finding, edit, and "
                    "journal and exits\nnonzero", text)

        chinese_paths = [
            Path("docs/zh-TW/WORKFLOW.md"), Path("docs/zh-TW/COMMANDS.md"),
            Path("docs/zh-TW/VERIFICATION.md"),
        ]
        chinese = "\n".join(_read(path) for path in chinese_paths)
        for phrase in (
                "REVIEW UNRESOLVED, HUMAN DECISION",
                "settling gate",
                "獨立結果",
                "exit code 為零"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_readme_auto_fix_contracts_stay_in_parity(self):
        """The two onboarding pages must expose the same opt-in boundaries."""
        readmes = {
            "README.md": _read(Path("README.md")),
            "README.zh-TW.md": _read(Path("README.zh-TW.md")),
        }
        required = {
            "README.md": (
                "## Optional bounded auto-fix",
                "`[auto_fix.review]`",
                "`assent run --auto-fix`",
                "An ordinary `assent run` without the flag starts neither review nor repair.",
                "read-only",
                "pre-existing technical debt",
                "directly interacting code",
                "unbounded repository-wide debt audit",
                "finite round bound",
                "never creates tasks",
                "reverts source",
                "deletes source",
                "accepts a folder",
                "`_auto_fix.toml`",
            ),
            "README.zh-TW.md": (
                "## 可選的有界 auto-fix",
                "`[auto_fix.review]`",
                "`assent run --auto-fix`",
                "沒有 flag 的普通 `assent run` 不會啟動 review，也不會 repair。",
                "唯讀",
                "既有 technical debt",
                "直接互動的程式碼",
                "全 repository debt audit",
                "round 的有限上界",
                "不會自動建立 task",
                "還原 source",
                "刪 source",
                "絕不自動接受 folder",
                "`_auto_fix.toml`",
            ),
        }
        for name, text in readmes.items():
            compact = " ".join(text.split())
            for phrase in required[name]:
                with self.subTest(readme=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        self.assertNotIn("Never accept or rework automatically", readmes["README.md"])
        self.assertNotIn("絕不要自動 accept 或 rework", readmes["README.zh-TW.md"])

    def test_orphaned_temporary_branch_sweep_stays_in_parity(self):
        """The English and zh-TW COMMANDS/OPERATIONS pages must match on the sweep."""
        english = {
            "docs/COMMANDS.md": _read(Path("docs/COMMANDS.md")),
            "docs/OPERATIONS.md": _read(Path("docs/OPERATIONS.md")),
        }
        required_english = {
            "docs/COMMANDS.md": (
                "## Orphaned temporary branch sweep",
                "assent-integration/<folder>/<suffix>",
                "assent-reconcile/<folder>",
                "reporting only",
                "once per invocation",
                "deliberately does not sweep",
            ),
            "docs/OPERATIONS.md": (
                "## `doctor`",
                "assent-integration/<folder>/<suffix>",
                "assent-reconcile/<folder>",
                "reporting information only",
                "once per invocation",
                "deliberately does not sweep",
                "`[y/N]`",
            ),
        }
        for name, text in english.items():
            compact = " ".join(text.split())
            for phrase in required_english[name]:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)

        chinese = {
            "docs/zh-TW/COMMANDS.md": _read(Path("docs/zh-TW/COMMANDS.md")),
            "docs/zh-TW/OPERATIONS.md": _read(Path("docs/zh-TW/OPERATIONS.md")),
        }
        required_chinese = {
            "docs/zh-TW/COMMANDS.md": (
                "## 孤兒暫存 branch 清理",
                "assent-integration/<folder>/<suffix>",
                "assent-reconcile/<folder>",
                "只是回報資訊",
                "刻意不掃",
            ),
            "docs/zh-TW/OPERATIONS.md": (
                "## `doctor`",
                "assent-integration/<folder>/<suffix>",
                "assent-reconcile/<folder>",
                "那只是回報資訊",
                "刻意不掃",
                "`[y/N]`",
            ),
        }
        for name, text in chinese.items():
            compact = " ".join(text.split())
            for phrase in required_chinese[name]:
                with self.subTest(document=name, phrase=phrase):
                    self.assertIn(phrase, compact)


if __name__ == "__main__":
    unittest.main()
