"""工作資料夾解析與寫回(格式契約見 templates/format.md)。

- 任務檔:tNNN_名稱.toml,表頭欄位嚴格驗證,未知鍵報錯。
- 日誌檔:tNNN_名稱.r.toml,append-only 的 [[entry]] 區塊。
- 對任務檔的機器寫入僅一種:set_status 精準替換 status 那一行,其餘位元組不動;
  寫完以 tomllib 重新解析驗證,防止多行字串裡的假 status 行被誤中。
"""
from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agents import AgentsError

_FILENAME_RE = re.compile(r"^t(\d{3})_(.+)\.toml$")
_ID_RE = re.compile(r"^t\d{3}$")
_STATUS_VALUES = {"TODO", "WIP", "DONE", "BLOCKED", "SKIP"}
_MODEL_TIERS = {"prime", "core", "lite"}
_EFFORT_LEVELS = {"low", "medium", "high"}
_KNOWN_KEYS = {"title", "deps", "model", "effort", "status", "scope", "verify",
               "goal", "behavior", "acceptance", "notes"}
_ENTRY_BY = {"codex", "claude", "scheduler"}
_ENTRY_AGENT = {"codex", "claude"}
# status 行:行首的 status = "VALUE"(容忍前置空白與行尾註解)
_STATUS_LINE_RE = re.compile(
    r'^(\s*status\s*=\s*")(TODO|WIP|DONE|BLOCKED|SKIP)("\s*(?:#.*)?)$')


@dataclass
class Task:
    id: str                        # 檔名前綴,如 "t001"(id 只存在於檔名)
    title: str
    deps: list[str]
    model: str                     # prime | core | lite
    effort: str | None             # low | medium | high;省略由 engine 套預設
    status: str                    # TODO | WIP | DONE | BLOCKED | SKIP
    scope: list[str]               # 允許改動路徑前綴;fail-closed,不可為空
    verify: str                    # 驗收命令
    goal: str
    behavior: str
    acceptance: str
    notes: str
    path: Path                     # 任務檔絕對路徑
    journal_path: Path             # 對應 .r.toml 日誌檔絕對路徑


def _require_str(data: dict, path: Path, key: str, *, allow_empty: bool = False) -> str:
    if key not in data:
        raise AgentsError(f"任務檔 {path.name} 缺少必填欄位:{key}")
    val = data[key]
    if not isinstance(val, str):
        raise AgentsError(f"任務檔 {path.name} 的 {key} 應為字串")
    if not allow_empty and not val.strip():
        raise AgentsError(f"任務檔 {path.name} 的 {key} 不可為空")
    return val


def _optional_str(data: dict, path: Path, key: str) -> str:
    if key not in data:
        return ""
    val = data[key]
    if not isinstance(val, str):
        raise AgentsError(f"任務檔 {path.name} 的 {key} 應為字串")
    return val


def _str_list(data: dict, path: Path, key: str) -> list[str]:
    if key not in data:
        raise AgentsError(f"任務檔 {path.name} 缺少必填欄位:{key}"
                          f"(無{'前置' if key == 'deps' else '限制'}也要明寫 [])")
    val = data[key]
    if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
        raise AgentsError(f"任務檔 {path.name} 的 {key} 應為字串陣列")
    return [x.strip() for x in val if x.strip()]


def journal_path_for(task_path: Path) -> Path:
    """t 檔路徑 -> 對應日誌路徑(任務檔完整主幹加上 .r.toml)。"""
    return task_path.with_suffix(".r.toml")


def parse_task_file(path: Path) -> Task:
    """解析單一任務檔;任何格式問題都以清楚訊息報錯(fail-closed)。"""
    if path.name.endswith(".r.toml"):
        raise AgentsError(
            f"日誌檔 {path.name} 不可當作任務檔解析"
            "(日誌檔格式為 tNNN_名稱.r.toml)")
    m = _FILENAME_RE.match(path.name)
    if m is None:
        raise AgentsError(
            f"任務檔名不合規則:{path.name}(需為 tNNN_名稱.toml,NNN 為三位數字)")
    task_id = f"t{m.group(1)}"

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise AgentsError(f"無法讀取任務檔 {path}:{e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AgentsError(f"任務檔 {path.name} 不是有效的 TOML:{e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise AgentsError(
            f"任務檔 {path.name} 含未定義的欄位:{', '.join(unknown)}"
            f"(有效欄位:{', '.join(sorted(_KNOWN_KEYS))})")

    status = _require_str(data, path, "status").strip()
    if status not in _STATUS_VALUES:
        raise AgentsError(
            f"任務檔 {path.name} 的 status = {status!r} 不合法"
            f"({' / '.join(sorted(_STATUS_VALUES))})")

    model = _require_str(data, path, "model").strip().lower()
    if model not in _MODEL_TIERS:
        raise AgentsError(
            f"任務檔 {path.name} 的 model = {model!r} 不是有效檔位"
            "(prime / core / lite;不要寫廠牌型號,對照表在 agents.toml)")

    effort_raw = _optional_str(data, path, "effort").strip().lower()
    if effort_raw and effort_raw not in _EFFORT_LEVELS:
        raise AgentsError(
            f"任務檔 {path.name} 的 effort = {effort_raw!r} 不合法"
            "(low / medium / high,或省略交由 agents.toml 預設)")

    deps = _str_list(data, path, "deps")
    for dep in deps:
        if not _ID_RE.match(dep):
            raise AgentsError(
                f"任務檔 {path.name} 的 deps 含不合法的任務 id:{dep!r}(需為 tNNN)")
        if dep == task_id:
            raise AgentsError(f"任務檔 {path.name} 的 deps 不可依賴自己")

    scope = _str_list(data, path, "scope")
    if not scope:
        raise AgentsError(
            f"任務檔 {path.name} 的 scope 為空:scope 檢查是 fail-closed"
            "(未宣告 = 任何變更都算越界),請明確列出允許改動的路徑")

    return Task(
        id=task_id,
        title=_require_str(data, path, "title").strip(),
        deps=deps,
        model=model,
        effort=effort_raw or None,
        status=status,
        scope=scope,
        verify=_require_str(data, path, "verify").strip(),
        goal=_require_str(data, path, "goal"),
        behavior=_optional_str(data, path, "behavior"),
        acceptance=_require_str(data, path, "acceptance"),
        notes=_optional_str(data, path, "notes"),
        path=path.resolve(),
        journal_path=journal_path_for(path.resolve()),
    )


class Plan:
    """一個工作資料夾的全部任務(檔名字典序)。"""

    def __init__(self, tasks: list[Task], tasks_dir: Path) -> None:
        self.tasks = tasks
        self.dir = tasks_dir

    @classmethod
    def parse(cls, tasks_dir: Path) -> "Plan":
        tasks_dir = Path(tasks_dir)
        if not tasks_dir.is_dir():
            raise AgentsError(
                f"找不到工作資料夾:{tasks_dir}"
                "(命令列參數或 agents.toml 的 [plan] tasks 指錯了?)")
        files = sorted(
            p for p in tasks_dir.iterdir()
            if (p.is_file() and not p.name.endswith(".r.toml")
                and _FILENAME_RE.match(p.name)))
        if not files:
            raise AgentsError(
                f"工作資料夾 {tasks_dir} 沒有任務檔(tNNN_名稱.toml);"
                "請先開 AI 會議產出計畫")

        tasks: list[Task] = []
        seen: dict[str, str] = {}
        for path in files:
            task = parse_task_file(path)
            if task.id in seen:
                raise AgentsError(
                    f"任務 id 重複:{task.id}({seen[task.id]} 與 {path.name})")
            seen[task.id] = path.name
            tasks.append(task)

        ids = {t.id for t in tasks}
        for task in tasks:
            for dep in task.deps:
                if dep not in ids:
                    raise AgentsError(
                        f"任務 {task.id} 依賴不存在的任務:{dep}"
                        "(檔案被改名或刪除了?deps 引用以檔名前綴為準)")
        cls._ensure_acyclic(tasks)
        return cls(tasks, tasks_dir)

    @staticmethod
    def _ensure_acyclic(tasks: list[Task]) -> None:
        deps_by_id = {t.id: t.deps for t in tasks}
        state: dict[str, int] = {}  # 0=未訪 1=訪問中 2=完成

        def visit(node: str, chain: list[str]) -> None:
            if state.get(node) == 2:
                return
            if state.get(node) == 1:
                cycle = " -> ".join(chain[chain.index(node):] + [node])
                raise AgentsError(f"任務依賴出現循環:{cycle}")
            state[node] = 1
            for dep in deps_by_id.get(node, []):
                visit(dep, chain + [node])
            state[node] = 2

        for task in tasks:
            visit(task.id, [])

    def get(self, task_id: str) -> Task | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def next_task(self) -> tuple[Task, bool] | None:
        """(任務, 是否為中斷續作)。WIP 優先(上次中斷的任務,帶接續提示重跑);
        其次由上而下第一個 TODO 且前置皆 DONE/SKIP;沒有則 None。"""
        for task in self.tasks:
            if task.status == "WIP":
                return task, True
        status_by_id = {t.id: t.status for t in self.tasks}
        for task in self.tasks:
            if task.status != "TODO":
                continue
            if all(status_by_id.get(dep) in ("DONE", "SKIP") for dep in task.deps):
                return task, False
        return None


def set_status(path: Path, new_status: str) -> None:
    """精準替換任務檔的 status 行,其餘位元組不動;寫回後重新解析驗證。"""
    if new_status not in _STATUS_VALUES:
        raise AgentsError(f"不合法的狀態:{new_status!r}")
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except OSError as e:
        raise AgentsError(f"無法讀取任務檔 {path}:{e}") from e

    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        m = _STATUS_LINE_RE.match(body)
        if m:
            lines[i] = f"{m.group(1)}{new_status}{m.group(3)}{eol}"
            break
    else:
        raise AgentsError(f"任務檔 {path.name} 找不到 status 行,無法寫回狀態")

    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("".join(lines))

    # 重新解析驗證:若剛才誤中多行字串裡的假 status 行,這裡會抓到不一致。
    with open(path, "rb") as f:
        data = tomllib.load(f)
    if data.get("status") != new_status:
        raise AgentsError(
            f"任務檔 {path.name} 狀態寫回後驗證失敗(可能有偽裝的 status 行);"
            "請人工檢查該檔")


def same_except_status(a: Task, b: Task) -> list[str]:
    """回傳兩份任務除 status 外不一致的欄位清單(空清單 = 一致)。

    調度器驗收用:執行 AI 對自己任務檔的合法修改只有 status 一行,
    其他欄位(deps/scope/verify/散文)被改動即視為越權(防放寬自己的驗收)。
    """
    diff = []
    for name in ("title", "deps", "model", "effort", "scope", "verify",
                 "goal", "behavior", "acceptance", "notes"):
        if getattr(a, name) != getattr(b, name):
            diff.append(name)
    return diff


def _toml_str(value: str) -> str:
    """單行 TOML basic string(JSON 字串跳脫與 TOML 相容)。"""
    return json.dumps(value, ensure_ascii=False)


def _toml_multiline(value: str) -> str:
    """多行 TOML literal string;''' 本身無法在其中表示,以相近字元替代。"""
    safe = value.replace("'''", "'' '")
    return f"'''\n{safe}\n'''"


def append_entry(journal: Path, *, by: str, event: str, summary: str,
                 detail: str = "", time_str: str | None = None,
                 agent: str | None = None,
                 requested_model: str | None = None) -> None:
    """在 .r.toml 日誌檔尾 append 一筆 [[entry]];不存在就建立。寫後解析驗證。

    ``agent`` 與 ``requested_model`` 是新版選填欄位;舊日誌仍由 ``read_entries``
    原樣讀取,但新寫入不再接受無法辨認 adapter 的籠統 ``by = "ai"``。
    """
    if by not in _ENTRY_BY:
        raise AgentsError(
            f"日誌 by 欄位不合法:{by!r}(codex / claude / scheduler)")
    if agent is not None and agent not in _ENTRY_AGENT:
        raise AgentsError(f"日誌 agent 欄位不合法:{agent!r}(codex / claude)")
    if requested_model is not None and not requested_model.strip():
        raise AgentsError("日誌 requested_model 不可為空字串")
    if time_str is None:
        time_str = datetime.now(timezone.utc).isoformat(timespec="seconds")

    block_lines = [
        "[[entry]]",
        f"time = {_toml_str(time_str)}",
        f"by = {_toml_str(by)}",
    ]
    if agent is not None:
        block_lines.append(f"agent = {_toml_str(agent)}")
    if requested_model is not None:
        block_lines.append(
            f"requested_model = {_toml_str(requested_model)}")
    block_lines += [
        f"event = {_toml_str(event)}",
        f"summary = {_toml_str(summary)}",
    ]
    if detail:
        block_lines.append(f"detail = {_toml_multiline(detail)}")
    block = "\n".join(block_lines) + "\n"

    existing = journal.read_text(encoding="utf-8") if journal.is_file() else ""
    with open(journal, "a", encoding="utf-8", newline="") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        if existing:
            f.write("\n")
        f.write(block)

    with open(journal, "rb") as f:
        try:
            tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AgentsError(
                f"日誌檔 {journal.name} append 後不是有效 TOML:{e}") from e


def read_entries(journal: Path) -> list[dict]:
    """讀取 .r.toml 全部 [[entry]];檔案不存在回空清單,壞檔回錯誤。report 用。"""
    if not journal.is_file():
        return []
    with open(journal, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AgentsError(f"日誌檔 {journal.name} 不是有效 TOML:{e}") from e
    entries = data.get("entry", [])
    return entries if isinstance(entries, list) else []
