"""assent init: generate the .assent skeleton and the AGENTS.md bridge notice
in a target project.

- .assent/assent.toml, instructions.md, format.md, verify.py. Work folders are
  not pre-created: their names are decided by a planning meeting based on the
  task at hand.
- AGENTS.md: create the project template if it does not exist; if it already
  exists, only append the one-line instructions bridge and leave the rest of
  the project's content untouched.
- .gitignore: excludes the whole .assent/; does not interfere with an existing
  AGENTS.md version-control choice.
"""
from __future__ import annotations

from pathlib import Path

from assent import AssentError

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_BRIDGE_MARKER = "<!-- assent-instructions -->"
_BRIDGE_LINE = (
    "- When using assent, first read `.assent/instructions.md` in the "
    "project's main worktree; a worktree session uses the absolute path the "
    f"scheduler provides. {_BRIDGE_MARKER}"
)
_GITIGNORE_LINES = [".assent/"]


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


def _merge_agents_md(root: Path, made: list[str], skipped: list[str]) -> None:
    target = root / "AGENTS.md"
    template = _template("AGENTS.md")
    if not target.exists():
        _create(target, template, made, skipped)
        return
    existing = target.read_text(encoding="utf-8")
    updated = existing
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

    assent_dir = root / ".assent"
    made: list[str] = []
    skipped: list[str] = []

    _create(assent_dir / "assent.toml", _template("assent.toml"), made, skipped)
    _create(assent_dir / "instructions.md", _template("instructions.md"), made, skipped)
    _create(assent_dir / "format.md", _template("format.md"), made, skipped)
    _create(assent_dir / "verify.py", _template("verify.py"), made, skipped)
    _merge_agents_md(root, made, skipped)
    _merge_gitignore(root, made)

    for item in made:
        print(f"Created: {item}")
    for item in skipped:
        print(f"Skipped (already exists): {item}")

    print()
    print("Next steps:")
    print("  1. Fill in AGENTS.md's project description and hard constraints, "
          "and the real check commands in .assent/verify.py")
    print("  2. Start an AI meeting: read .assent/instructions.md and begin an "
          "assent planning meeting")
    print("  3. The meeting names a work folder for the task at hand (e.g. "
          ".assent/loginfix01/) and produces task files inside it "
          "(format in .assent/format.md)")
    print("  4. Once assent check passes, run assent run")
    return 0
