"""CLI 進入點:argparse 子命令 run / status / check / report / init。"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

from agents import AgentsError, engine
from agents.config import list_task_folders, load_config, validate_config
from agents.init import init as run_init
from agents.plan import Plan
from agents.terminal_log import terminal_logging

_DEFAULT_CONFIG = ".agents/agents.toml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents",
        description="AI 計畫格式 + 自動調度器:讀 .agents 工作資料夾、"
                    "逐任務開 AI session、客觀驗收、自動 git 檢查點。",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{run,status,check,report,init}")

    run_p = sub.add_parser("run", help="執行指定[工作資料夾]的任務直到全部 DONE/BLOCKED/SKIP")
    run_p.add_argument("--once", action="store_true", help="只執行下一個任務後停止")
    run_p.add_argument("--task", metavar="ID", help="指定執行單一任務(仍檢查前置)")

    status_p = sub.add_parser("status", help="顯示指定[工作資料夾]的進度統計與下一個任務(零 token)")
    check_p = sub.add_parser("check", help="驗證指定[工作資料夾]的任務檔格式、設定檔與環境(零 token;"
                                           "會議的散會條件)")
    report_p = sub.add_parser("report", help="生成人讀的執行報告 _report.md(零 token)")

    init_p = sub.add_parser("init", help="在專案生成 .agents 骨架與 AGENTS.md")
    init_p.add_argument("--path", default=".", metavar="DIR",
                        help="目標專案根目錄(預設:目前目錄)")

    for p in (run_p, status_p, check_p, report_p):
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
    """依任務現況選唯一進行中資料夾;任何歧義或壞檔皆拒絕猜測。"""
    plans: list[tuple[str, Plan]] = []
    errors: list[tuple[str, str]] = []
    for folder in folders:
        try:
            cfg = load_config(config_path, folder)
            plans.append((folder, Plan.parse(cfg.tasks_dir)))
        except AgentsError as e:
            errors.append((folder, str(e)))

    ongoing = [folder for folder, plan in plans
               if any(task.status in ("TODO", "WIP") for task in plan.tasks)]
    if len(ongoing) == 1 and not errors:
        selected = ongoing[0]
        print(f"工作資料夾:{selected}(唯一進行中,自動選定)")
        return selected

    print(f"無法自動選定工作資料夾:進行中資料夾共 {len(ongoing)} 個。")
    print("工作資料夾狀態:")
    if not plans and not errors:
        print("  (未找到含任務檔的工作資料夾)")
    for folder, plan in plans:
        print(f"  {folder}: {_status_summary(plan)}")
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


def _dispatch(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "init":
        return run_init(args.path)

    try:
        agents_dir = validate_config(args.config)
    except AgentsError as e:
        print(f"設定檔錯誤:{e}")
        return 1

    folders = list_task_folders(agents_dir)
    if args.folder is None:
        if args.command == "run":
            folder = _select_run_folder(args.config, folders)
            if folder is None:
                return 1
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


def main(argv: list[str] | None = None) -> int:
    # Windows 下 stdout/stderr 導向管線/檔案時預設用系統 code page,中文會變亂碼
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    with terminal_logging(actual_argv):
        return _dispatch(actual_argv)


if __name__ == "__main__":
    sys.exit(main())
