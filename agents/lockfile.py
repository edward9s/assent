"""工作資料夾檔案鎖:同一工作資料夾同時只允許一個 agents run。

run 啟動時對工作資料夾取得 OS 層「非阻塞獨占鎖」(Windows 用 msvcrt、POSIX 用
fcntl),進程存活期間持有。鎖的生命週期綁定檔案控制代碼:進程無論如何終止
(crash / kill / Ctrl+C),OS 都會自動釋放——因此不留 stale lock、無 PID 重用
問題,也不需要任何人工清理。這正是不採「PID lock file + 存活檢查」方案的原因。

鎖檔為 <tasks_dir>/agents.lock,平時留在磁碟、永不刪除(刪檔會引入 race)。
檔內只寫 PID、啟動時間、資料夾名供診斷,不作為判斷依據。

限制:網路檔案系統上 flock / msvcrt.locking 的語意不可靠(部分 NFS、SMB
實作不保證跨主機互斥);本鎖只保證本機同一檔案系統上的互斥。
"""
from __future__ import annotations

import contextlib
import os
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from agents import AgentsError

LOCK_NAME = "agents.lock"

# Windows 的 msvcrt.locking 是強制鎖(mandatory):會擋掉他人對該區段的「讀取」。
# 把鎖放在遠離內容的高位元組、永不寫入該處,搶輸的一方才讀得到檔頭的診斷內容
# (Windows 允許鎖定 EOF 之後的區段,不會撐大檔案)。POSIX flock 是勸告鎖、
# 且鎖整個開啟描述,offset 對它無意義,一併沿用不影響。
_LOCK_OFFSET = 1 << 30  # 1 GiB
_LOCK_BYTES = 1


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle) -> bool:
        handle.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        handle.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
        except OSError:
            pass
else:
    import fcntl

    def _try_lock(handle) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class LockBusy(AgentsError):
    """工作資料夾已被另一個 run 持鎖;訊息已含先行者的 PID 與資料夾名。"""


class LockMissing(AgentsError):
    """鎖檔不存在；不建立檔案的呼叫端無法安全取得同一把鎖。"""


def _write_diag(handle, tasks_name: str) -> None:
    """把 PID、啟動時間(ISO 8601)、資料夾名寫入鎖檔(truncate 重寫,僅供診斷)。"""
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    body = (
        f"pid = {os.getpid()}\n"
        f'started_at = "{started}"\n'
        f'folder = "{tasks_name}"\n'
    )
    handle.seek(0)
    handle.truncate()
    handle.write(body.encode("utf-8"))
    handle.flush()


def _read_diag(path: Path) -> dict:
    """讀鎖檔診斷內容;讀不到或壞格式一律回空 dict(診斷不影響互斥判斷)。"""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _short_time(started_at) -> str:
    """ISO 8601 時間字串取 HH:MM 供訊息用;取不到回空字串。"""
    if not isinstance(started_at, str):
        return ""
    try:
        return datetime.fromisoformat(started_at).strftime("%H:%M")
    except ValueError:
        return ""


def _busy_message(tasks_name: str, diag: dict) -> str:
    pid = diag.get("pid")
    detail = ""
    if isinstance(pid, int):
        detail = f"(PID {pid}"
        hhmm = _short_time(diag.get("started_at"))
        if hhmm:
            detail += f",自 {hhmm} 起"
        detail += ")"
    return (f"另一個 agents run 正在處理工作資料夾 {tasks_name}{detail}。"
            "同一工作資料夾同時只能有一個 run。")


@contextlib.contextmanager
def hold_lock(tasks_dir: Path, tasks_name: str) -> Iterator[None]:
    """對 <tasks_dir>/agents.lock 取得 OS 層非阻塞獨占鎖,持有至離開 with 區塊。

    搶不到鎖:讀出鎖檔診斷內容,拋 LockBusy(訊息含先行者 PID 與資料夾名);
    呼叫端據此以退出碼 1 失敗,不碰工作區任何東西。取鎖成功即把診斷內容寫回鎖檔。
    """
    tasks_dir = Path(tasks_dir)
    tasks_dir.mkdir(parents=True, exist_ok=True)
    path = tasks_dir / LOCK_NAME
    # O_CREAT 但不 O_TRUNC:建立缺檔又不截斷既有內容(截斷會砸掉持鎖者寫的診斷)。
    # 走二進位:鎖要 seek 到 _LOCK_OFFSET 這種大位移,text 串流只認 tell() 回傳的
    # 不透明 cookie、不接受任意位移;O_BINARY 同時避開 Windows 的換行轉換。
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    handle = os.fdopen(os.open(str(path), flags, 0o644), "r+b")
    try:
        if not _try_lock(handle):
            raise LockBusy(_busy_message(tasks_name, _read_diag(path)))
        _write_diag(handle, tasks_name)
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()


@contextlib.contextmanager
def probe_lock(tasks_dir: Path, tasks_name: str) -> Iterator[None]:
    """取得既有工作資料夾鎖，但完全不建立或改寫 ``agents.lock``。

    ``clean`` 必須與 ``run`` 使用同一把鎖，卻又不得碰觸 ``.agents/`` 的計畫
    歸檔。鎖檔不存在時若先建立再刪除，會在釋鎖與刪檔之間留下競態；因此直接
    保守拒絕，由呼叫端跳過清理。正常曾執行過 ``run`` 的資料夾已有鎖檔。
    """
    path = Path(tasks_dir) / LOCK_NAME
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(str(path), flags)
    except FileNotFoundError as e:
        raise LockMissing(
            f"工作資料夾 {tasks_name} 沒有既有 {LOCK_NAME}，"
            "無法在不改動 .agents 的前提下證明未被鎖") from e
    except OSError as e:
        raise AgentsError(f"無法開啟工作資料夾 {tasks_name} 的鎖檔:{e}") from e

    handle = os.fdopen(descriptor, "r+b")
    try:
        if not _try_lock(handle):
            raise LockBusy(_busy_message(tasks_name, _read_diag(path)))
        try:
            yield
        finally:
            _unlock(handle)
    finally:
        handle.close()
