"""工作資料夾層級的依賴解析、完成推導與循環檢查。

- ``_folder.toml`` 只宣告 ``after``；缺檔等同沒有資料夾前置。
- 資料夾完成與否每次由正式任務檔現場推導，不另寫狀態檔。
- 本模組只提供能力；執行與檢查命令的閘門整合由呼叫端負責。
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from agents import AgentsError
from agents.config import _validate_tasks_name, list_task_folders
from agents.plan import Plan

_FOLDER_CONFIG_NAME = "_folder.toml"
_KNOWN_KEYS = {"after"}


@dataclass(frozen=True)
class FolderDependencies:
    """一個工作資料夾的前置資料夾宣告。"""

    name: str
    after: list[str]
    path: Path


@dataclass(frozen=True)
class FolderCompletion:
    """從任務檔推導出的資料夾完成結果與原因。"""

    complete: bool
    reason: str


def parse_folder_dependencies(tasks_dir: str | Path) -> FolderDependencies:
    """解析並驗證工作資料夾的 ``_folder.toml``。

    被引用的工作資料夾必須是同一個 ``.agents`` 目錄下、含正式任務檔的
    資料夾。缺少 ``_folder.toml`` 時回傳空的 ``after``。
    """
    tasks_dir = Path(tasks_dir)
    if not tasks_dir.is_dir():
        raise AgentsError(f"找不到工作資料夾:{tasks_dir}")

    name = tasks_dir.name
    _validate_tasks_name(name, "工作資料夾名稱")
    path = tasks_dir / _FOLDER_CONFIG_NAME
    if not path.is_file():
        return FolderDependencies(name=name, after=[], path=path.resolve())

    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except OSError as e:
        raise AgentsError(f"無法讀取資料夾依賴檔 {path}:{e}") from e
    except tomllib.TOMLDecodeError as e:
        raise AgentsError(
            f"資料夾依賴檔 {path} 不是有效的 TOML:{e}") from e

    unknown = sorted(set(data) - _KNOWN_KEYS)
    if unknown:
        raise AgentsError(
            f"資料夾依賴檔 {path} 含未知鍵:{', '.join(unknown)}"
            f"(有效鍵:{', '.join(sorted(_KNOWN_KEYS))})")
    if "after" not in data:
        raise AgentsError(
            f"資料夾依賴檔 {path} 缺少 after"
            "(無前置資料夾也要明寫 after = [])")

    after = data["after"]
    if not isinstance(after, list) or not all(isinstance(item, str) for item in after):
        raise AgentsError(f"資料夾依賴檔 {path} 的 after 應為字串陣列")

    available = set(list_task_folders(tasks_dir.parent))
    for dependency in after:
        _validate_tasks_name(dependency, f"資料夾 {name} 的 after 元素")
        if dependency == name:
            raise AgentsError(f"資料夾 {name} 的 after 不可依賴自己")
        if dependency not in available:
            raise AgentsError(
                f"資料夾 {name} 的 after 引用不存在或沒有任務檔的工作資料夾:"
                f"{dependency}")

    return FolderDependencies(
        name=name, after=list(after), path=path.resolve())


def infer_folder_completion(tasks_dir: str | Path) -> FolderCompletion:
    """現場解析任務檔，推導資料夾是否全部為 ``DONE`` 或 ``SKIP``。"""
    try:
        plan = Plan.parse(Path(tasks_dir))
    except AgentsError as e:
        return FolderCompletion(False, f"無法推導資料夾完成狀態:{e}")

    unfinished = [
        f"{task.id}={task.status}"
        for task in plan.tasks
        if task.status not in ("DONE", "SKIP")
    ]
    if unfinished:
        return FolderCompletion(False, f"尚未完成的任務:{', '.join(unfinished)}")
    return FolderCompletion(True, "全部任務皆為 DONE 或 SKIP")


def parse_folder_dependency_graph(
        agents_dir: str | Path) -> dict[str, FolderDependencies]:
    """解析全部工作資料夾的 ``after`` 圖並檢查循環。"""
    agents_dir = Path(agents_dir)
    dependencies = {
        name: parse_folder_dependencies(agents_dir / name)
        for name in list_task_folders(agents_dir)
    }
    _ensure_acyclic(dependencies)
    return dependencies


def _ensure_acyclic(dependencies: dict[str, FolderDependencies]) -> None:
    """檢查資料夾依賴圖，循環訊息包含首尾相接的完整路徑。"""
    state: dict[str, int] = {}  # 0=未訪 1=訪問中 2=完成

    def visit(node: str, chain: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = " -> ".join(chain[chain.index(node):] + [node])
            raise AgentsError(f"資料夾依賴出現循環:{cycle}")
        state[node] = 1
        for dependency in dependencies[node].after:
            visit(dependency, chain + [node])
        state[node] = 2

    for name in dependencies:
        visit(name, [])
