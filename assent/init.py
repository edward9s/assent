"""assent init: generate the .agents skeleton and the AGENTS.md bridge notice
in a target project.

- .agents/agents.toml, instructions.md, format.md, verify.py. Work folders are
  not pre-created: their names are decided by a planning meeting based on the
  task at hand.
- AGENTS.md: create the project template if it does not exist; if it already
  exists, only append the one-line instructions bridge. The legacy
  "AI working system" section is removed; the rest of the project's content is
  left untouched.
- .gitignore: excludes the whole .agents/; does not interfere with an existing
  AGENTS.md version-control choice.
"""
from __future__ import annotations

import re
from pathlib import Path

from assent import AssentError

_TEMPLATES = Path(__file__).resolve().parent / "templates"
# Heading of the section that older, pre-English releases of the former CLI
# wrote into a project's AGENTS.md. It is legacy-detection data, not user-facing
# prose, so the historical characters are kept as escapes and removed on re-init.
_LEGACY_SECTION_MARKER = "## AI \u5de5\u4f5c\u9ad4\u7cfb(.agents)"
_BRIDGE_MARKER = "<!-- agents-instructions -->"
_BRIDGE_LINE = (
    "- When using assent, first read `.agents/instructions.md` in the "
    "project's main worktree; a worktree session uses the absolute path the "
    f"scheduler provides. {_BRIDGE_MARKER}"
)
_GITIGNORE_LINES = [".agents/"]


def _template(name: str) -> str:
    path = _TEMPLATES / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise AssentError(
            f"Cannot read built-in template {name}: {e} (broken install?)") from e


def _create(path: Path, content: str, made: list[str], skipped: list[str]) -> None:
    if path.exists():
        skipped.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    made.append(str(path))


def _remove_legacy_section(text: str) -> str:
    """Remove the legacy scheduler-generated level-2 section, keeping any
    project-owned level-2 section that follows it."""
    start = text.find(_LEGACY_SECTION_MARKER)
    if start < 0:
        return text
    line_start = text.rfind("\n", 0, start) + 1
    body_start = text.find("\n", start)
    if body_start < 0:
        body_start = len(text)
    else:
        body_start += 1
    match = re.search(r"(?m)^## ", text[body_start:])
    end = body_start + match.start() if match else len(text)
    before = text[:line_start].rstrip()
    after = text[end:].lstrip("\r\n")
    return before + (("\n\n" + after) if after else "") + "\n"


def _merge_agents_md(root: Path, made: list[str], skipped: list[str]) -> None:
    target = root / "AGENTS.md"
    template = _template("AGENTS.md")
    if not target.exists():
        _create(target, template, made, skipped)
        return
    existing = target.read_text(encoding="utf-8")
    updated = _remove_legacy_section(existing)
    if _BRIDGE_MARKER not in updated:
        updated = updated.rstrip() + "\n\n" + _BRIDGE_LINE + "\n"
    if updated == existing:
        skipped.append(f"{target} (already has the instructions bridge)")
        return
    target.write_text(updated, encoding="utf-8", newline="\n")
    made.append(f"{target} (updated the instructions bridge)")


def _merge_gitignore(root: Path, made: list[str]) -> None:
    target = root / ".gitignore"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    lines = existing.splitlines()
    have = {line.strip() for line in lines}
    missing = [line for line in _GITIGNORE_LINES if line not in have]
    if not missing:
        return
    if missing:
        if lines and lines[-1]:
            lines.append("")
        if lines:
            lines.append("# assent management surface and runtime output")
        lines.extend(missing)
    target.write_text("\n".join(lines) + ("\n" if lines else ""),
                      encoding="utf-8", newline="\n")
    made.append(f"{target} ({len(missing)} line(s) added)")


def init(path: str | Path = ".") -> int:
    root = Path(path).resolve()
    if not root.is_dir():
        print(f"Error: directory does not exist: {root}")
        return 1
    if not (root / ".git").exists():
        print("This project has no git repository yet; run git init first")
        return 1

    agents_dir = root / ".agents"
    made: list[str] = []
    skipped: list[str] = []

    _create(agents_dir / "agents.toml", _template("agents.toml"), made, skipped)
    _create(agents_dir / "instructions.md", _template("instructions.md"), made, skipped)
    _create(agents_dir / "format.md", _template("format.md"), made, skipped)
    _create(agents_dir / "verify.py", _template("verify.py"), made, skipped)
    _merge_agents_md(root, made, skipped)
    _merge_gitignore(root, made)

    for item in made:
        print(f"Created: {item}")
    for item in skipped:
        print(f"Skipped (already exists): {item}")

    print()
    print("Next steps:")
    print("  1. Fill in AGENTS.md's project description and hard constraints, "
          "and the real check commands in .agents/verify.py")
    print("  2. Start an AI meeting: read .agents/instructions.md and begin an "
          "assent planning meeting")
    print("  3. The meeting names a work folder for the task at hand (e.g. "
          ".agents/loginfix01/) and produces task files inside it "
          "(format in .agents/format.md)")
    print("  4. Once assent check passes, run assent run")
    return 0
