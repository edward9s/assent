"""CLI 進入點:argparse 子命令 run / status / check / report / clean / reject / init。"""
from __future__ import annotations

import argparse
import signal
import sys
from collections import Counter

from agents import AgentsError, engine
from agents.clean import clean_folders
from agents.config import list_task_folders, load_config, validate_config
from agents.folderdeps import (find_unfinished_prerequisites,
                               parse_folder_dependency_graph)
from agents.folder_scheduler import run_all
from agents.init import init as run_init
from agents.plan import Plan
from agents.reject import reject_folder
from agents.terminal_log import terminal_logging

_DEFAULT_CONFIG = ".agents/agents.toml"


def _positive_int(value: str) -> int:
    """解析大於零的命令列整數。"""
    try:
        number = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("必須是整數") from e
    if number < 1:
        raise argparse.ArgumentTypeError("必須大於 0")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents",
        description="AI 計畫格式 + 自動調度器:讀 .agents 工作資料夾、"
                    "逐任務開 AI session、客觀驗收、自動 git 檢查點。",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{run,status,check,report,clean,reject,init}")

    run_p = sub.add_parser("run", help="執行指定[工作資料夾]的任務直到全部 DONE/BLOCKED/SKIP")
    run_p.add_argument("--once", action="store_true", help="只執行下一個任務後停止")
    run_p.add_argument("--task", metavar="ID", help="指定執行單一任務(仍檢查前置)")
    run_p.add_argument("--all", action="store_true", dest="all_folders",
                       help="依資料夾依賴順序執行全部未完成工作資料夾")
    run_p.add_argument("--jobs", type=_positive_int, metavar="N",
                       help="--all 同時執行的資料夾數上限(預設:1)")

    status_p = sub.add_parser("status", help="顯示指定[工作資料夾]的進度統計與下一個任務(零 token)")
    check_p = sub.add_parser("check", help="驗證指定[工作資料夾]的任務檔格式、設定檔與環境(零 token;"
                                           "會議的散會條件)")
    report_p = sub.add_parser("report", help="生成人讀的執行報告 _report.md(零 token)")
    clean_p = sub.add_parser("clean", help="清除可證明冗餘的 worktree 與已併入分支")

    reject_p = sub.add_parser("reject", help="人工裁決駁回:封存後強制清除該資料夾的 "
                                             "worktree 與分支,任務改回 TODO")
    reject_p.add_argument("folder", metavar="FOLDER",
                          help="要駁回的工作資料夾(必填,不可作用於全部資料夾)")
    reject_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                          help=f"設定檔位置(預設:{_DEFAULT_CONFIG})")

    init_p = sub.add_parser("init", help="在專案生成 .agents 骨架與 AGENTS.md")
    init_p.add_argument("--path", default=".", metavar="DIR",
                        help="目標專案根目錄(預設:目前目錄)")

    for p in (run_p, status_p, check_p, report_p, clean_p):
        p.add_argument("folder", nargs="?", metavar="FOLDER",
                       help="指定工作資料夾;省略時 run 自動推導,其餘命令作用於全部資料夾")
        p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=f"設定檔位置(預設:{_DEFAULT_CONFIG})")
    return parser


def _status_summary(plan: Plan) -> str:
    counts = Counter(task.status for task in plan.tasks)
    return (f"DONE {counts.get('DONE', 0)} / "
            f"BLOCKED {counts.get('BLOCKED', 0)} / "
            f"SKIP {counts.get('SKIP', 0)} / "
            f"WIP {counts.get('WIP', 0)} / "
            f"TODO {counts.get('TODO', 0)}(共 {len(plan.tasks)})")


def _select_run_folder(config_path: str, folders: list[str]) -> str | None:
    """依任務與前置現況選唯一可跑資料夾；歧義或壞檔皆拒絕猜測。"""
    plans: list[tuple[str, Plan, list[str]]] = []
    errors: list[tuple[str, str]] = []
    for folder in folders:
        try:
            cfg = load_config(config_path, folder)
            plan = Plan.parse(cfg.tasks_dir)
            waiting = [item.name for item in
                       find_unfinished_prerequisites(cfg.tasks_dir)]
            plans.append((folder, plan, waiting))
        except AgentsError as e:
            errors.append((folder, str(e)))

    runnable = [folder for folder, plan, waiting in plans
                if (any(task.status in ("TODO", "WIP") for task in plan.tasks)
                    and not waiting)]
    if len(runnable) == 1 and not errors:
        selected = runnable[0]
        print(f"工作資料夾:{selected}(唯一進行中且可跑,自動選定)")
        return selected

    print(f"無法自動選定工作資料夾:進行中且可跑資料夾共 {len(runnable)} 個。")
    print("工作資料夾狀態:")
    if not plans and not errors:
        print("  (未找到含任務檔的工作資料夾)")
    for folder, plan, waiting in plans:
        reason = f"(等待 {'、'.join(waiting)})" if waiting and any(
            task.status in ("TODO", "WIP") for task in plan.tasks) else ""
        print(f"  {folder}: {_status_summary(plan)}{reason}")
    for folder, error in errors:
        print(f"  {folder}: 無法解析({error})")
    print("請明寫工作資料夾參數:agents run <folder>")
    return None


def _dispatch_all(command: str, config_path: str, folders: list[str]) -> int:
    """依序對全部工作資料夾執行唯讀命令,彙總退出碼。"""
    if not folders:
        print("找不到含任務檔的工作資料夾。")
        return 1
    operation = getattr(engine, command)
    result = 0
    for index, folder in enumerate(folders):
        if index:
            print()
        try:
            cfg = load_config(config_path, folder)
        except AgentsError as e:
            print(f"設定檔錯誤:{e}")
            result = 1
            continue
        if operation(cfg) != 0:
            result = 1
    return result


def _dispatch_check_all(config_path: str, agents_dir, folders: list[str]) -> int:
    """驗證全部資料夾本身，並額外驗證完整依賴圖與循環。"""
    graph_ok = True
    try:
        graph = parse_folder_dependency_graph(agents_dir)
        print(f"資料夾依賴圖:OK({len(graph)} 個工作資料夾,引用完整且無循環)")
    except AgentsError as e:
        graph_ok = False
        print(f"資料夾依賴圖:FAIL({e})")
    checks_ok = _dispatch_all("check", config_path, folders) == 0
    return 0 if graph_ok and checks_ok else 1


def _dispatch(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.all_folders and args.folder is not None:
            parser.error("run 的 --all 與 FOLDER 不可同時使用")
        if args.all_folders and (args.once or args.task is not None):
            parser.error("run 的 --all 不可與 --once 或 --task 同時使用")
        if not args.all_folders and args.jobs is not None:
            parser.error("run 的 --jobs 只能與 --all 同時使用")

    if args.command == "init":
        return run_init(args.path)

    try:
        agents_dir = validate_config(args.config)
    except AgentsError as e:
        print(f"設定檔錯誤:{e}")
        return 1

    if args.command == "run" and args.all_folders:
        return run_all(args.config, agents_dir, args.jobs or 1)
    if args.command == "reject":
        try:
            cfg = load_config(args.config, args.folder)
        except AgentsError as e:
            print(f"設定檔錯誤:{e}")
            return 1
        return reject_folder(cfg)
    folders = list_task_folders(agents_dir)
    if args.command == "clean":
        selected = folders if args.folder is None else [args.folder]
        if not selected:
            print("找不到含任務檔的工作資料夾。")
            return 1
        configs = []
        for selected_folder in selected:
            try:
                configs.append(load_config(args.config, selected_folder))
            except AgentsError as e:
                print(f"設定檔錯誤:{e}")
                return 1
        return clean_folders(configs)
    if args.folder is None:
        if args.command == "run":
            folder = _select_run_folder(args.config, folders)
            if folder is None:
                return 1
        elif args.command == "check":
            return _dispatch_check_all(args.config, agents_dir, folders)
        else:
            return _dispatch_all(args.command, args.config, folders)
    else:
        folder = args.folder

    try:
        cfg = load_config(args.config, folder)
    except AgentsError as e:
        print(f"設定檔錯誤:{e}")
        return 1

    if args.command == "run":
        return engine.run(cfg, once=args.once, task_id=args.task)
    if args.command == "status":
        return engine.status(cfg)
    if args.command == "check":
        return engine.check(cfg)
    if args.command == "report":
        return engine.report(cfg)
    return 2  # argparse required=True 已擋住,防禦性保底


def _install_break_handler() -> None:
    """Windows 限定:讓 CTRL_BREAK_EVENT 轉為 KeyboardInterrupt。

    ``run --all`` 以 CREATE_NEW_PROCESS_GROUP 啟動子行程,中斷時只能送
    CTRL_BREAK_EVENT(對應 SIGBREAK)。子行程若未註冊處理器,收到訊號會被 OS
    直接終止(退出碼 3221225786),engine 的中斷收尾(WIP 標記、r 檔 interrupt
    條目、wip 檢查點)完全不會執行,違反「燒過 tokens 的產出絕不丟棄」。改綁
    default_int_handler 後,SIGBREAK 會走與 Ctrl+C 相同的 KeyboardInterrupt 路徑。
    POSIX 無 SIGBREAK,行為不變。
    """
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


def main(argv: list[str] | None = None) -> int:
    _install_break_handler()
    # Windows 下 stdout/stderr 導向管線/檔案時預設用系統 code page,中文會變亂碼
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    with terminal_logging(actual_argv):
        return _dispatch(actual_argv)


if __name__ == "__main__":
    sys.exit(main())
