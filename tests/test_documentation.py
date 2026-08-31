"""Regression checks for concise reader documentation."""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
TOPICS = ("WORKFLOW", "COMMANDS", "CONFIGURATION", "VERIFICATION",
          "OPERATIONS")
LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)(?:\s+[^)]*)?\)")


def _read(relative: Path) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    return "".join(text.split())


def _reader_surfaces() -> list[Path]:
    paths = [Path("README.md"), Path("README.zh-TW.md")]
    paths.extend(path.relative_to(ROOT) for path in (ROOT / "docs").rglob("*.md"))
    return paths


def _relative_markdown_targets(text: str) -> list[str]:
    targets = []
    for match in LINK_RE.finditer(text):
        target = unquote(match.group(1)).split("#", 1)[0]
        if (not target or target.startswith(("#", "<", "~", "/"))
                or "://" in target or target.startswith("mailto:")):
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
        if part not in {child.name for child in current.iterdir()}:
            return False
        current /= part
    return current.is_file()


class DocumentationTests(unittest.TestCase):
    def test_guides_have_reciprocal_english_and_chinese_links(self):
        for topic in TOPICS:
            english = Path("docs") / f"{topic}.md"
            translated = Path("docs/zh-TW") / f"{topic}.md"
            with self.subTest(topic=topic):
                self.assertTrue((ROOT / english).is_file())
                self.assertTrue((ROOT / translated).is_file())
                self.assertIn(f"zh-TW/{topic}.md", _read(english))
                self.assertIn(f"../{topic}.md", _read(translated))

    def test_all_relative_markdown_links_resolve_case_sensitively(self):
        for relative in _reader_surfaces():
            source = ROOT / relative
            for target in _relative_markdown_targets(_read(relative)):
                resolved = (source.parent / target).resolve()
                try:
                    relative_target = resolved.relative_to(ROOT)
                except ValueError:
                    relative_target = Path("__outside_repository__")
                with self.subTest(source=relative, target=target):
                    self.assertTrue(_case_sensitive_file(relative_target),
                                    f"broken link: {target}")

    def test_readmes_cover_installation_retention_and_human_acceptance(self):
        for relative in (Path("README.md"), Path("README.zh-TW.md")):
            text = _read(relative)
            lower = _flat(text.lower())
            with self.subTest(path=relative):
                self.assertIn(_flat("python -m pip install assent"), lower)
                self.assertIn(_flat("python -m pip uninstall assent"), lower)
                for term in ("~/.assent", ".assent/", "worktree", "archive",
                             "git branch", "assent accept"):
                    self.assertIn(_flat(term), lower)
                self.assertRegex(text, r"(?:does not delete|不會刪除)")

        self.assertIn(
            "Turn the consensus above into an Assent-format plan under "
            "`.assent/<PLAN>/`.", _read(Path("README.md")))
        self.assertIn(
            "將上述討論的共識，建立成 `.assent/<PLAN>/` 下的 Assent 格式計畫。",
            _read(Path("README.zh-TW.md")))

    def test_reader_guides_explain_the_three_stage_workflow(self):
        english = _read(Path("README.md")) + _read(Path("docs/WORKFLOW.md"))
        for phrase in (
                "Planning meeting", "Unattended execution",
                "Acceptance review", "focused_test", "focused_sweep",
                "full_verify", "REVIEW UNRESOLVED, HUMAN DECISION",
                "No workflow step accepts a plan"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(phrase, english)

        chinese = (_read(Path("README.zh-TW.md"))
                   + _read(Path("docs/zh-TW/WORKFLOW.md")))
        for phrase in (
                "規劃會議", "自動執行", "驗收", "focused_test",
                "focused_sweep", "full_verify",
                "REVIEW UNRESOLVED, HUMAN DECISION"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(phrase, chinese)

    def test_configuration_guides_are_workflow_first_references(self):
        english = " ".join(_read(Path("docs/CONFIGURATION.md")).split())
        for phrase in (
                "An ability has a prompt and a write capability",
                "Ability names have no engine meaning",
                "A passing action completes the layer",
                "There is no structured verdict setting",
                "a preflight repair array, three core finite step arrays",
                "preflight", "runtime_test",
                "integration_repairer", "Task workflow overrides",
                '{ role = "tests_writer" }',
                '{ action = "focused_test" }'):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(" ".join(phrase.split()), english)

        chinese = " ".join(_read(Path("docs/zh-TW/CONFIGURATION.md")).split())
        for phrase in (
                "Ability 只有 prompt 與寫入能力",
                "Ability 名稱\n對 engine 沒有特殊意義",
                "Action 通過就完成該層",
                "設定中沒有 structured verdict",
                "preflight repair array、三個核心且長度有限的 step array",
                "preflight", "runtime_test",
                "integration_repairer", "Task workflow override",
                '{ role = "tests_writer" }',
                '{ action = "focused_test" }'):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(" ".join(phrase.split()), chinese)

    def test_planning_and_acceptance_prompts_keep_human_boundaries(self):
        english = _flat(_read(Path("docs/WORKFLOW.md")))
        for phrase in (
                "Do not overengineer", "After explicit human agreement",
                "create no files before I explicitly agree",
                "turn the consensus above into an Assent-format plan",
                "independent acceptance reviewer",
                "Report evidence-based bugs", "do not accept, rework, or edit anything",
                "Wait for the human decision"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(_flat(phrase), english)

        chinese = _flat(_read(Path("docs/zh-TW/WORKFLOW.md")))
        for phrase in (
                "不要過度設計", "人類明確同意", "在我明確同意前不要建立檔案",
                "將上述討論的共識", "Assent 格式計畫",
                "獨立驗收者", "不要自行 accept、rework 或修改檔案",
                "等待人類決定"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(_flat(phrase), chinese)

    def test_selection_and_verification_choices_are_discoverable(self):
        english = (_read(Path("docs/COMMANDS.md"))
                   + _read(Path("docs/VERIFICATION.md")))
        for phrase in (
                "assent run --jobs 2",
                "One selected plan", "one exact batch",
                "assent verify <PLAN> --focus", "Direct and selected acceptance",
                "assent ignored-dirs status",
                "Running `declare` in the primary worktree",
                "cannot safely link everything ignored by Git"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(phrase, english)

        chinese = _flat(_read(Path("docs/zh-TW/COMMANDS.md"))
                        + _read(Path("docs/zh-TW/VERIFICATION.md")))
        for phrase in (
                "assent run --jobs 2", "一個 plan",
                "精確 batch", "assent verify <PLAN> --focus",
                "assent ignored-dirs status",
                "在主要 worktree 執行 `declare`",
                "不能把所有 ignored directory 都建立成鏈結"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(_flat(phrase), chinese)

    def test_runtime_test_command_and_configuration_are_discoverable(self):
        english = " ".join(" ".join((
            _read(Path("README.md")),
            _read(Path("docs/COMMANDS.md")),
            _read(Path("docs/CONFIGURATION.md")),
            _read(Path("docs/WORKFLOW.md")),
            _read(Path("docs/OPERATIONS.md")),
        )).split())
        for phrase in (
                "assent test [PLAN]", "assent test <PLAN>",
                "_runtime_test.toml", "[runtime_test].command",
                "execution = \"after_plan\"", "runtime_repairer",
                "ordered command array", "stops at its first failed command",
                "full_verify", "source-bound", "REVIEW UNRESOLVED, HUMAN DECISION"):
            with self.subTest(language="English", phrase=phrase):
                self.assertIn(" ".join(phrase.split()), english)

        chinese = " ".join(" ".join((
            _read(Path("README.zh-TW.md")),
            _read(Path("docs/zh-TW/COMMANDS.md")),
            _read(Path("docs/zh-TW/CONFIGURATION.md")),
            _read(Path("docs/zh-TW/WORKFLOW.md")),
            _read(Path("docs/zh-TW/OPERATIONS.md")),
        )).split())
        for phrase in (
                "assent test [PLAN]", "assent test <PLAN>",
                "_runtime_test.toml", "[runtime_test].command",
                "execution = \"after_plan\"", "runtime_repairer",
                "有序 command array", "第一個失敗 command 停止",
                "full_verify", "source-bound", "REVIEW UNRESOLVED, HUMAN DECISION"):
            with self.subTest(language="Traditional Chinese", phrase=phrase):
                self.assertIn(" ".join(phrase.split()), chinese)


    def test_reader_guides_distinguish_rework_from_destructive_reject(self):
        english = _flat(_read(Path("docs/WORKFLOW.md")))
        chinese = _flat(_read(Path("docs/zh-TW/WORKFLOW.md")))
        for phrase in ("reopens an existing task while preserving code",
                       "confirmed destructive reset", "resets started tasks to `TODO`"):
            self.assertIn(_flat(phrase), english)
        for phrase in ("保留程式碼並重開既有 task", "破壞性重設",
                       "已開始的 task 重設為 `TODO`"):
            self.assertIn(_flat(phrase), chinese)

    def test_translation_guide_does_not_require_manual_version_bookkeeping(self):
        text = _read(Path("docs/TRANSLATING.md"))
        self.assertIn("natural Traditional Chinese used in Taiwan", text)
        self.assertIn("Git already records that history", " ".join(text.split()))
        self.assertNotIn("translated commit", text.lower())
        self.assertNotIn("short hash", text.lower())


if __name__ == "__main__":
    unittest.main()
