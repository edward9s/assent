"""agents init:在目標專案生成 .agents 骨架與 AGENTS.md(只建立,絕不覆蓋)。

- .agents/agents.toml、.agents/format.md、.agents/verify.py、預設工作資料夾。
- AGENTS.md:不存在 -> 整檔複製;已存在且缺「AI 工作體系」一節 -> 整段 append;
  已有該節 -> 不動。
- .gitignore:補上整個 .agents/,已有就不重複加。
"""
from __future__ import annotations

from pathlib import Path

from agents import AgentsError

_TEMPLATES = Path(__file__).resolve().parent / "templates"
_SECTION_MARKER = "## AI 工作體系"
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


def _merge_agents_md(root: Path, made: list[str], skipped: list[str]) -> None:
    target = root / "AGENTS.md"
    template = _template("AGENTS.md")
    if not target.exists():
        _create(target, template, made, skipped)
        return
    existing = target.read_text(encoding="utf-8")
    if _SECTION_MARKER in existing:
        skipped.append(f"{target}(已含「{_SECTION_MARKER}」一節)")
        return
    idx = template.find(_SECTION_MARKER)
    if idx < 0:  # 範本損壞的防禦;正常情況不會發生
        raise AgentsError("內建 AGENTS.md 範本缺少「AI 工作體系」一節")
    section = template[idx:]
    with open(target, "a", encoding="utf-8", newline="\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n" + section)
    made.append(f"{target}(append「{_SECTION_MARKER}」一節)")


def _merge_gitignore(root: Path, made: list[str]) -> None:
    target = root / ".gitignore"
    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    have = {line.strip() for line in existing.splitlines()}
    missing = [line for line in _GITIGNORE_LINES if line not in have]
    if not missing:
        return
    with open(target, "a", encoding="utf-8", newline="\n") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if existing:
            f.write("\n# agents 執行期產物\n")
        f.write("\n".join(missing) + "\n")
    made.append(f"{target}(補 {len(missing)} 行)")


def init(path: str | Path = ".") -> int:
    root = Path(path).resolve()
    if not root.is_dir():
        print(f"錯誤:目錄不存在:{root}")
        return 1

    agents_dir = root / ".agents"
    made: list[str] = []
    skipped: list[str] = []

    _create(agents_dir / "agents.toml", _template("agents.toml"), made, skipped)
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
    print(f"  2. 開 AI 會議產出任務檔到 .agents/{_DEFAULT_FOLDER}/"
          "(格式見 .agents/format.md;資料夾名可改,同步改 agents.toml)")
    print("  3. agents check 通過後,agents run")
    return 0
