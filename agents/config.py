"""agents.toml 讀取、工作資料夾列舉與驗證。

- agents.toml 位於專案的 .agents/ 內;專案根目錄 = .agents 的上層目錄。
- 工作資料夾名稱由呼叫端提供;git 分支前綴 = 該名稱 + "/"。
- 未提供的欄位套預設值;未知的頂層鍵一律報錯(防打錯字靜默失效)。
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from agents import AgentsError
from agents.lockfile import LOCK_NAME

_TOP_LEVEL_KEYS = {"watchdog", "run", "adapter", "prompt"}
_EFFORT_LEVELS = {"low", "medium", "high"}

# 工作資料夾名稱:不含空白與路徑分隔符,不以 - 或 . 開頭(它會成為 git 分支前綴)
_FOLDER_RE = re.compile(r"^[^\s/\\]+$")
_TASK_FILE_RE = re.compile(r"^t\d{3}_.+\.e\.toml$")

_DEFAULT_EXTRA_ARGS = ["--permission-mode", "acceptEdits"]
# 抽象檔位 -> claude CLI --model 參數
_DEFAULT_MODELS = {"prime": "fable", "core": "opus", "lite": "sonnet"}
_DEFAULT_EFFORT = {"prime": "high", "core": "high", "lite": "medium"}

_DEFAULT_CODEX_EXTRA_ARGS = ["--sandbox", "workspace-write"]
_DEFAULT_CODEX_MODELS = {
    "prime": "gpt-5.6-sol", "core": "gpt-5.6-terra", "lite": "gpt-5.6-luna",
}
_DEFAULT_CODEX_EFFORT = {"prime": "high", "core": "medium", "lite": "low"}


@dataclass
class Config:
    root: Path                     # 專案根目錄 = .agents 的上層
    agents_dir: Path               # .agents 目錄(= 設定檔所在目錄)
    tasks_dir: Path                # 工作資料夾(.agents/<tasks>)
    tasks_name: str                # 工作資料夾名稱(= git 分支前綴的字首)
    stall_minutes: int = 30        # 0 = 關閉 watchdog
    retry_per_task: int = 1
    quota_poll_minutes: int = 30
    adapter_name: str = "claude"
    claude_command: str = "claude"
    claude_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_EXTRA_ARGS))
    claude_models: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_MODELS))
    claude_default_effort: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_EFFORT))
    codex_command: str = "codex"
    codex_extra_args: list[str] = field(
        default_factory=lambda: list(_DEFAULT_CODEX_EXTRA_ARGS))
    codex_models: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_CODEX_MODELS))
    codex_default_effort: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_CODEX_EFFORT))
    prompt_template: str | None = None
    source_root: Path | None = None  # 隔離執行時的原始主工作樹;不來自設定檔

    @property
    def branch_prefix(self) -> str:
        return f"{self.tasks_name}/"

    def rel(self, path: Path) -> str:
        """供提示詞使用的路徑;專案內用相對路徑,外部真本則用絕對路徑。"""
        resolved = path.resolve()
        try:
            return resolved.relative_to(self.root.resolve()).as_posix()
        except ValueError:
            if self.source_root is not None:
                resolved.relative_to(self.source_root.resolve())
                return str(resolved)
            raise

    def git_rel(self, path: Path) -> str:
        """把主樹或 worktree 內的路徑轉成 repo 相對路徑,供 git pathspec。"""
        resolved = path.resolve()
        roots = (self.root, self.source_root) if self.source_root else (self.root,)
        for root in roots:
            try:
                return resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                continue
        raise ValueError(f"路徑不在專案工作樹內:{resolved}")

    def for_worktree(self, root: Path) -> "Config":
        """派生只把程式碼/git 根目錄移入 worktree 的等效執行設定。"""
        return replace(self, root=root.resolve(), source_root=self.root.resolve())

    @property
    def runtime_log_rel(self) -> str:
        return self.git_rel(self.tasks_dir / "_agents.log")

    @property
    def report_rel(self) -> str:
        return self.git_rel(self.tasks_dir / "_report.md")

    @property
    def lockfile_rel(self) -> str:
        return self.git_rel(self.tasks_dir / LOCK_NAME)

    @property
    def git_excludes(self) -> tuple[str, ...]:
        """執行期產物:不參與乾淨檢查、scope 檢查與 checkpoint commit。"""
        return (self.runtime_log_rel, self.report_rel, self.lockfile_rel)


def _section(data: dict, name: str) -> dict:
    val = data.get(name, {})
    if not isinstance(val, dict):
        raise AgentsError(f"設定檔 [{name}] 應為表(table),不是純值")
    return val


def _typed(section: dict, owner: str, key: str, typ: type, default):
    if key not in section:
        return default
    val = section[key]
    if not isinstance(val, typ) or (typ is not bool and isinstance(val, bool)):
        raise AgentsError(f"設定檔 {owner} 的 {key} 型別錯誤:應為 {typ.__name__}")
    return val


def _str_list(section: dict, owner: str, key: str, default: list[str]) -> list[str]:
    val = _typed(section, owner, key, list, None)
    if val is None:
        return list(default)
    if not all(isinstance(x, str) for x in val):
        raise AgentsError(f"設定檔 {owner} 的 {key} 每個元素都應為字串")
    return list(val)


def _str_map(section: dict, owner: str, key: str, default: dict[str, str]) -> dict[str, str]:
    val = _typed(section, owner, key, dict, None)
    if val is None:
        return dict(default)
    if not all(isinstance(v, str) for v in val.values()):
        raise AgentsError(f"設定檔 [{owner}.{key}] 每個值都應為字串")
    return dict(val)


def _validate_tasks_name(tasks_name: str, owner: str) -> None:
    """驗證工作資料夾名稱，確保它可安全作為 git 分支前綴。"""
    if not _FOLDER_RE.match(tasks_name) or tasks_name[0] in "-.":
        raise AgentsError(
            f"{owner} = {tasks_name!r} 不是合法的工作資料夾名稱"
            "(不可含空白或路徑分隔符,不可以 - 或 . 開頭;它同時是 git 分支前綴)")


def _load_data(path: str | Path) -> tuple[Path, dict]:
    """讀取並驗證不依賴工作資料夾的設定內容。"""
    path = Path(path)
    if not path.is_file():
        raise AgentsError(
            f"找不到設定檔:{path}(還沒初始化?請在專案根目錄執行 agents init)")
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise AgentsError(f"設定檔不是有效的 TOML({path}):{e}") from e

    if "git" in data:
        raise AgentsError(
            "設定檔 [git] 區塊已廢除:git 永遠啟用,請刪除該區塊")
    if "plan" in data:
        raise AgentsError(
            "設定檔 [plan] 區塊已廢除:"
            "工作資料夾改由命令列指定或自動推導,請刪除該區塊")

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise AgentsError(
            f"設定檔含未知的頂層鍵:{', '.join(unknown)}"
            f"(有效鍵:{', '.join(sorted(_TOP_LEVEL_KEYS))})")

    return path.resolve(), data


def validate_config(path: str | Path) -> Path:
    """驗證設定檔並回傳其所在的 ``.agents`` 目錄。"""
    resolved, _ = _load_data(path)
    return resolved.parent


def list_task_folders(agents_dir: str | Path) -> list[str]:
    """列出含正式任務檔的工作資料夾,名稱依字典序排列。"""
    agents_dir = Path(agents_dir)
    if not agents_dir.is_dir():
        return []
    folders = []
    for entry in agents_dir.iterdir():
        if (not entry.is_dir() or entry.name == "__pycache__"
                or entry.name.startswith("_")):
            continue
        if any(child.is_file() and _TASK_FILE_RE.match(child.name)
               for child in entry.iterdir()):
            folders.append(entry.name)
    return sorted(folders)


def load_config(path: str | Path, folder: str) -> Config:
    """載入設定,並以呼叫端提供的工作資料夾名稱建構衍生路徑。"""
    resolved, data = _load_data(path)
    _validate_tasks_name(folder, "命令列工作資料夾")

    agents_dir = resolved.parent
    root = agents_dir.parent

    tasks_name = folder

    watchdog = _section(data, "watchdog")
    run = _section(data, "run")
    adapter = _section(data, "adapter")
    claude = _section(adapter, "claude") if "claude" in adapter else {}
    codex = _section(adapter, "codex") if "codex" in adapter else {}
    prompt = _section(data, "prompt")

    cfg = Config(
        root=root,
        agents_dir=agents_dir,
        tasks_dir=agents_dir / tasks_name,
        tasks_name=tasks_name,
        stall_minutes=_typed(watchdog, "[watchdog]", "stall_minutes", int, 30),
        retry_per_task=_typed(run, "[run]", "retry_per_task", int, 1),
        quota_poll_minutes=_typed(run, "[run]", "quota_poll_minutes", int, 30),
        adapter_name=_typed(adapter, "[adapter]", "name", str, "claude"),
        claude_command=_typed(claude, "[adapter.claude]", "command", str, "claude"),
        claude_extra_args=_str_list(claude, "[adapter.claude]", "extra_args",
                                    _DEFAULT_EXTRA_ARGS),
        claude_models=_str_map(claude, "adapter.claude", "models", _DEFAULT_MODELS),
        claude_default_effort=_str_map(claude, "adapter.claude", "default_effort",
                                       _DEFAULT_EFFORT),
        codex_command=_typed(codex, "[adapter.codex]", "command", str, "codex"),
        codex_extra_args=_str_list(codex, "[adapter.codex]", "extra_args",
                                   _DEFAULT_CODEX_EXTRA_ARGS),
        codex_models=_str_map(codex, "adapter.codex", "models",
                              _DEFAULT_CODEX_MODELS),
        codex_default_effort=_str_map(codex, "adapter.codex", "default_effort",
                                      _DEFAULT_CODEX_EFFORT),
        prompt_template=_typed(prompt, "[prompt]", "template", str, None),
    )

    if cfg.stall_minutes < 0:
        raise AgentsError("[watchdog] stall_minutes 不可為負(0 = 關閉)")
    if cfg.retry_per_task < 0:
        raise AgentsError("[run] retry_per_task 不可為負")
    if cfg.quota_poll_minutes < 1:
        raise AgentsError("[run] quota_poll_minutes 至少為 1")
    for owner, efforts in (
            ("adapter.claude", cfg.claude_default_effort),
            ("adapter.codex", cfg.codex_default_effort)):
        for model, eff in efforts.items():
            if eff not in _EFFORT_LEVELS:
                raise AgentsError(
                    f"[{owner}.default_effort] {model} = {eff!r} 不是有效 effort"
                    f"({'/'.join(sorted(_EFFORT_LEVELS))})")
    return cfg
