"""agents init:在目標專案生成 .agents 骨架與 AGENTS.md 橋接指示。

- .agents/agents.toml、instructions.md、format.md、verify.py、預設工作資料夾。
- AGENTS.md:不存在 -> 建立專案範本;已存在 -> 只補一行 instructions 橋接。
  舊版「AI 工作體系」區塊會移除,其他專案內容不動。
- .gitignore:排除整個 .agents/;既有的 AGENTS.md 版控選擇不干涉。
"""
from __future__ import annotations

import re
from pathlib import Path

from agents import AgentsError

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_LEGACY_SECTION_MARKER = "## AI 工作體系(.agents)"
_BRIDGE_MARKER = "<!-- agents-instructions -->"
_BRIDGE_LINE = (
    "- 使用 agents 時,請先讀專案主工作樹的 `.agents/instructions.md`;"
    "worktree session 以調度器提示的絕對路徑為準。 "
    f"{_BRIDGE_MARKER}"
)
_DEFAULT_FOLDER = "plan01"
_GITIGNORE_LINES = [".agents/"]


def _template(name: str) -> str:
    path = _TEMPLATES / name
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise AgentsError(f"讀不到內建範本 {name}:{e}(安裝損壞?)") from e


def _create(path: Path, content: str, made: list[str], skipped: list[str]) -> None:
    if path.exists():
        skipped.append(str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    made.append(str(path))


def _remove_legacy_section(text: str) -> str:
    """移除舊版由 agents 產生的二級節,保留後續專案自有的二級節。"""
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
        skipped.append(f"{target}(已含 instructions 橋接)")
        return
    target.write_text(updated, encoding="utf-8", newline="\n")
    made.append(f"{target}(更新 instructions 橋接)")


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
            lines.append("# agents 管理面與執行期產物")
        lines.extend(missing)
    target.write_text("\n".join(lines) + ("\n" if lines else ""),
                      encoding="utf-8", newline="\n")
    made.append(f"{target}(補 {len(missing)} 行)")


def init(path: str | Path = ".") -> int:
    root = Path(path).resolve()
    if not root.is_dir():
        print(f"錯誤:目錄不存在:{root}")
        return 1
    if not (root / ".git").exists():
        print("本專案尚未初始化 git,請先執行 git init")
        return 1

    agents_dir = root / ".agents"
    made: list[str] = []
    skipped: list[str] = []

    _create(agents_dir / "agents.toml", _template("agents.toml"), made, skipped)
    _create(agents_dir / "instructions.md", _template("instructions.md"), made, skipped)
    _create(agents_dir / "format.md", _template("format.md"), made, skipped)
    _create(agents_dir / "verify.py", _template("verify.py"), made, skipped)
    (agents_dir / _DEFAULT_FOLDER).mkdir(parents=True, exist_ok=True)
    _merge_agents_md(root, made, skipped)
    _merge_gitignore(root, made)

    for item in made:
        print(f"已建立:{item}")
    for item in skipped:
        print(f"略過(已存在):{item}")

    print()
    print("接下來:")
    print("  1. 填寫 AGENTS.md 的專案描述與硬限制、.agents/verify.py 的實際檢查命令")
    print("  2. 開 AI 會議:請讀 .agents/instructions.md,開始 agents 規劃會議")
    print(f"  3. 會議產出任務檔到 .agents/{_DEFAULT_FOLDER}/"
          "(格式見 .agents/format.md;資料夾名可改,同步改 agents.toml)")
    print("  4. agents check 通過後,agents run")
    return 0
