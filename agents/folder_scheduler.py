"""``run --all`` 的工作資料夾層級調度。

單一工作資料夾仍交給新的 ``agents run <folder>`` 子行程處理；本模組只負責
依賴解鎖、平行數上限、單行摘要與中斷轉送。
"""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TextIO

from agents import AgentsError
from agents.folderdeps import parse_folder_dependency_graph
from agents.plan import Plan

_POLL_SECONDS = 0.05
_GIT_REQUIRED_MESSAGE = "本專案尚未初始化 git,請先執行 git init"
# 130 是子行程正常走 KeyboardInterrupt 收尾後的退出碼;3221225786(0xC000013A,
# STATUS_CONTROL_C_EXIT)是 Windows 上仍未裝處理器的路徑被 OS 直接終止的碼——
# 兩者都是「已中斷」而非「資料夾失敗」,避免誤報。
_INTERRUPT_RETURNCODES = (130, 3221225786)


def _start_folder(config_path: str, folder: str) -> subprocess.Popen:
    """啟動等同 ``agents run <folder>`` 的隔離子行程。"""
    command = [
        sys.executable, "-m", "agents", "run", folder,
        "--config", str(Path(config_path).resolve()),
    ]
    kwargs = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "bufsize": 1,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _read_folder_output(
        folder: str, stream: TextIO,
        output: queue.Queue[tuple[str, str]]) -> None:
    """持續排空單一子行程管線,把完整實體列送回家長執行緒。"""
    try:
        for line in stream:
            output.put((folder, line))
    finally:
        stream.close()


def _start_output_reader(
        folder: str, process: subprocess.Popen,
        output: queue.Queue[tuple[str, str]]) -> threading.Thread | None:
    """為具有 stdout 管線的子行程啟動背景讀取執行緒。"""
    stream = getattr(process, "stdout", None)
    if stream is None:
        # 測試替身與第三方直接呼叫可能只實作最小 Popen 介面。
        return None
    reader = threading.Thread(
        target=_read_folder_output,
        args=(folder, stream, output),
        name=f"agents-output-{folder}",
        daemon=True,
    )
    reader.start()
    return reader


def _write_folder_line(folder: str, line: str) -> None:
    """把一列子行程訊息只寫到家長終端,沒有專用通道時退回 stdout。"""
    content = line.removesuffix("\n").removesuffix("\r")
    stream = sys.stdout
    write = getattr(stream, "write_terminal_only", stream.write)
    write(f"[{folder}] {content}\n")
    stream.flush()


def _drain_output(output: queue.Queue[tuple[str, str]]) -> None:
    """由家長執行緒序列化目前已收到的完整輸出列。"""
    while True:
        try:
            folder, line = output.get_nowait()
        except queue.Empty:
            return
        _write_folder_line(folder, line)


def _finish_folder_output(
        folder: str, readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]]) -> None:
    """等待指定管線讀到 EOF,並在完成摘要前排空最後輸出。"""
    reader = readers.pop(folder, None)
    if reader is not None:
        reader.join()
    _drain_output(output)


def _folder_plans(agents_dir: Path, folders: list[str]) -> dict[str, Plan]:
    """重新解析全部正式任務檔；任一壞檔都直接拒絕繼續調度。"""
    return {folder: Plan.parse(agents_dir / folder) for folder in folders}


def _is_complete(plan: Plan) -> bool:
    return all(task.status in ("DONE", "SKIP") for task in plan.tasks)


def _has_ongoing(plan: Plan) -> bool:
    return any(task.status in ("TODO", "WIP") for task in plan.tasks)


def _blocking_chains(folder: str, graph, plans: dict[str, Plan]) -> list[str]:
    """列出從資料夾一路指向 BLOCKED 任務的卡住鏈。"""
    plan = plans[folder]
    chains = [
        f"{folder} -> {task.id}(BLOCKED)"
        for task in plan.tasks if task.status == "BLOCKED"
    ]
    for dependency in graph[folder].after:
        if _is_complete(plans[dependency]):
            continue
        for chain in _blocking_chains(dependency, graph, plans):
            chains.append(f"{folder} -> {chain}")
    if not chains:
        statuses = "、".join(
            f"{task.id}={task.status}" for task in plan.tasks
            if task.status not in ("DONE", "SKIP"))
        chains.append(f"{folder} -> 尚未完成({statuses})")
    return chains


def _print_stuck(graph, plans: dict[str, Plan]) -> None:
    """明列所有未完成資料夾及其無法解鎖的原因。"""
    print("無法繼續:剩餘工作資料夾皆因 BLOCKED 而無法解鎖:")
    for folder in graph:
        if _is_complete(plans[folder]):
            continue
        for chain in _blocking_chains(folder, graph, plans):
            print(f"  - {chain}")


def _send_interrupt(process: subprocess.Popen) -> None:
    """只向本次啟動的子行程群組轉送使用者中斷。"""
    if process.poll() is not None:
        return
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        os.killpg(process.pid, signal.SIGINT)


def _interrupt_and_wait(
        active: dict[str, subprocess.Popen],
        readers: dict[str, threading.Thread],
        output: queue.Queue[tuple[str, str]]) -> None:
    """轉送中斷後等待各子行程自行保存現場並退出。"""
    print("\n收到中斷(Ctrl+C):通知執行中的工作資料夾自行收尾...")
    for folder, process in active.items():
        try:
            _send_interrupt(process)
            print(f"中斷工作資料夾:{folder}")
        except (OSError, ValueError) as e:
            print(f"中斷訊號轉送失敗:{folder}({e})")
    for folder, process in active.items():
        try:
            returncode = process.wait()
            _finish_folder_output(folder, readers, output)
            print(f"工作資料夾已收尾:{folder}(退出碼 {returncode})")
        except OSError as e:
            print(f"等待工作資料夾收尾失敗:{folder}({e})")
    _drain_output(output)


def run_all(config_path: str, agents_dir: str | Path, jobs: int = 1) -> int:
    """依資料夾依賴圖執行全部未完成工作資料夾。"""
    agents_dir = Path(agents_dir)
    if not (agents_dir.parent / ".git").exists():
        print(_GIT_REQUIRED_MESSAGE)
        return 1
    active: dict[str, subprocess.Popen] = {}
    readers: dict[str, threading.Thread] = {}
    output: queue.Queue[tuple[str, str]] = queue.Queue()
    attempted: set[str] = set()
    failure = False
    try:
        while True:
            try:
                graph = parse_folder_dependency_graph(agents_dir)
                if not graph:
                    print("找不到含任務檔的工作資料夾。")
                    return 1
                inactive = [folder for folder in graph if folder not in active]
                # 其他子行程可能正在寫自己的任務檔；只解析非執行中資料夾。
                plans = _folder_plans(agents_dir, inactive)
            except AgentsError as e:
                print(f"資料夾調度失敗:{e}")
                return 1

            if not active and all(_is_complete(plan) for plan in plans.values()):
                print("全部工作資料夾已完成(DONE/SKIP)。")
                return 0

            runnable = [
                folder for folder, dependencies in graph.items()
                if folder not in active
                and folder not in attempted
                and _has_ongoing(plans[folder])
                and all(name not in active and _is_complete(plans[name])
                        for name in dependencies.after)
            ]
            while not failure and runnable and len(active) < jobs:
                folder = runnable.pop(0)
                try:
                    process = _start_folder(config_path, folder)
                    active[folder] = process
                    reader = _start_output_reader(folder, process, output)
                    if reader is not None:
                        readers[folder] = reader
                except OSError as e:
                    print(f"工作資料夾啟動失敗:{folder}({e})")
                    failure = True
                    break
                print(f"啟動工作資料夾:{folder}")

            if not active:
                if failure:
                    return 1
                _print_stuck(graph, plans)
                return 1

            completed: list[tuple[str, int]] = []
            while not completed:
                _drain_output(output)
                for folder, process in active.items():
                    returncode = process.poll()
                    if returncode is not None:
                        completed.append((folder, returncode))
                if not completed:
                    time.sleep(_POLL_SECONDS)

            interrupted = False
            for folder, returncode in completed:
                del active[folder]
                attempted.add(folder)
                _finish_folder_output(folder, readers, output)
                if returncode == 0:
                    print(f"完成工作資料夾:{folder}(退出碼 0)")
                elif returncode in _INTERRUPT_RETURNCODES:
                    print(f"工作資料夾已中斷:{folder}(退出碼 {returncode})")
                    interrupted = True
                else:
                    log_path = agents_dir / folder / "_agents.log"
                    print(f"工作資料夾失敗:{folder}(退出碼 {returncode};"
                          f"詳情見 {log_path})")
                    failure = True
            if interrupted:
                _interrupt_and_wait(active, readers, output)
                return 130
            if failure and not active:
                return 1
    except KeyboardInterrupt:
        _interrupt_and_wait(active, readers, output)
        return 130
