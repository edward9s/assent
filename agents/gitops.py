"""git 操作:分支、乾淨/scope 檢查、commit、還原。

全部以 cwd=專案根目錄執行 git,回傳前先檢查 returncode;git 缺席時給清楚錯誤訊息
而非 traceback。`excludes` 為執行期產物的相對路徑清單(.agents/agents.log、
report.md 等):它們永遠不是輸入也不是檢查點內容,故不參與乾淨檢查、scope 檢查
與 commit。
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from agents import AgentsError


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # core.quotepath=false:非 ASCII 檔名(中文等)git 預設會用八進位跳脫成純 ASCII
    # (如 "\346\270\254\350\251\246.txt"),關掉後直接輸出原始 UTF-8 檔名。
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args], cwd=root,
            capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise AgentsError("找不到 git 執行檔;請確認 git 已安裝並在 PATH 中") from e


def _git(root: Path, *args: str) -> str:
    result = _run_git(root, *args)
    if result.returncode != 0:
        raise AgentsError(
            f"git {' '.join(args)} 失敗(退出碼 {result.returncode}):"
            f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _describe_change(line: str) -> str:
    """把一行 git status --porcelain 翻成人看得懂的「狀態:路徑」。"""
    code = line[:2]
    path = line[3:].strip().strip('"')
    if " -> " in path:                       # rename:"old -> new",取 new
        path = path.split(" -> ", 1)[1].strip().strip('"')
    if code == "??":
        label = "未追蹤(新檔)"
    elif "R" in code:
        label = "已改名"
    elif "A" in code:
        label = "已加入索引"
    elif "D" in code:
        label = "已刪除"
    elif "M" in code:
        label = "已修改"
    else:
        label = f"變更({code.strip() or code})"
    return f"{label}:{path}"


def _normalize(path_str: str) -> str:
    return path_str.replace("\\", "/")


def _status_path(line: str) -> str:
    path = line[3:].strip().strip('"')
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip().strip('"')
    return _normalize(path)


def _meaningful_status_lines(output: str, excludes: Sequence[str]) -> list[str]:
    excluded = {_normalize(e) for e in excludes}
    return [line for line in output.splitlines()
            if line.strip() and _status_path(line) not in excluded]


def ensure_clean(root: Path, excludes: Sequence[str] = ()) -> None:
    """工作樹不乾淨(含未追蹤檔)-> raise AgentsError。"""
    out = _git(root, "status", "--porcelain")
    lines = _meaningful_status_lines(out, excludes)
    if lines:
        detail = "\n".join(f"  - {_describe_change(ln)}" for ln in lines)
        raise AgentsError(
            "工作樹不乾淨,無法繼續(請先 commit 這些變更,或把不該進版控的檔加入 "
            f".gitignore):\n{detail}")


def ensure_branch(root: Path, prefix: str) -> str:
    """已在 <prefix> 分支上則沿用,否則從目前分支建 <prefix><UTC 時間戳>。"""
    current = _git(root, "branch", "--show-current").strip()
    if current.startswith(prefix):
        return current
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    branch = f"{prefix}{run_id}"
    _git(root, "checkout", "-b", branch)
    return branch


def changes_outside_scope(root: Path, scope: list[str],
                          since_ref: str | None = None,
                          excludes: Sequence[str] = ()) -> list[str]:
    """回傳落在 scope 之外的變更路徑清單(工作區現況 + 選擇性含已 commit 的變更)。

    - since_ref 給定時,額外把 since_ref..HEAD 之間已 commit 的變更(額度中斷/重試
      期間建立的 wip 檢查點)也納入檢查,確保「保留進度不還原」後 scope 檢查
      仍涵蓋該任務自起點以來的全部改動。
    - scope 為空清單 = fail-closed,任何變更皆視為越界。這是刻意設計:圍堵無人
      看管的執行 AI 是 scope 存在的目的,「沒寫 = 不限制」會讓保護悄悄失效。
      (任務檔解析已強制 scope 非空,這裡的 fail-closed 是最後防線。)
    """
    excluded = {_normalize(e) for e in excludes}
    paths: list[str] = []
    out = _git(root, "status", "--porcelain")
    for line in out.splitlines():
        if not line.strip():
            continue
        path_part = _status_path(line)
        if path_part not in excluded:
            paths.append(path_part)
    if since_ref:
        diff = _git(root, "diff", "--name-only", since_ref, "HEAD")
        paths += [p.strip().strip('"') for p in diff.splitlines()
                  if p.strip() and _normalize(p.strip().strip('"')) not in excluded]

    normalized_scope = [_normalize(s) for s in scope]
    outside: list[str] = []
    seen: set[str] = set()
    for path_part in paths:
        path_norm = _normalize(path_part)
        if path_norm in seen:
            continue
        seen.add(path_norm)
        if not any(path_norm == s or path_norm.startswith(s.rstrip("/") + "/")
                   for s in normalized_scope):
            outside.append(path_part)
    return outside


def commit_all(root: Path, message: str, excludes: Sequence[str] = ()) -> None:
    """git add -A(排除執行期產物)&& git commit -m message。"""
    spec = ["--", "."] + [f":(exclude){e}" for e in excludes]
    _git(root, "add", "-A", *spec)
    _git(root, "commit", "-m", message)


def commit_if_dirty(root: Path, message: str, excludes: Sequence[str] = ()) -> bool:
    """工作樹有任何變更(含未追蹤檔)才 commit;回傳是否真的建立了 commit。

    engine 用它保留進度(額度中斷/使用者中斷的 wip 檢查點):tokens 已經燒掉,
    產出絕不能丟——這是「以降低 tokens 消耗為優先」的直接推論。
    """
    out = _git(root, "status", "--porcelain")
    if not _meaningful_status_lines(out, excludes):
        return False
    commit_all(root, message, excludes)
    return True


def head_ref(root: Path) -> str | None:
    """目前 HEAD 的 commit hash;無法取得(如空 repo)回 None。"""
    result = _run_git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def restore(root: Path) -> None:
    """丟棄工作區全部未 commit 變更:checkout -- . && clean -fd。

    注意:這會刪掉執行 AI 已經燒 tokens 做出的產出,engine 的正常流程一律不呼叫;
    保留給人工救援用。
    """
    _git(root, "checkout", "--", ".")
    _git(root, "clean", "-fd")
