"""CLI 進入點:argparse 子命令 run / status / check / report / init。"""
from __future__ import annotations

import argparse
import sys

from agents import AgentsError, engine
from agents.config import load_config
from agents.init import init as run_init
from agents.terminal_log import terminal_logging

_DEFAULT_CONFIG = ".agents/agents.toml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents",
        description="AI 計畫格式 + 零 token 自動調度器:讀 .agents 工作資料夾、"
                    "逐任務開 AI session、客觀驗收、自動 git 檢查點。",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{run,status,check,report,init}")

    run_p = sub.add_parser("run", help="執行任務直到全部 DONE/BLOCKED/SKIP")
    run_p.add_argument("--once", action="store_true", help="只執行下一個任務後停止")
    run_p.add_argument("--task", metavar="ID", help="指定執行單一任務(仍檢查前置)")

    status_p = sub.add_parser("status", help="顯示進度統計與下一個任務(零 token)")
    check_p = sub.add_parser("check", help="驗證任務檔格式、設定檔與環境(零 token;"
                                           "會議的散會條件)")
    report_p = sub.add_parser("report", help="生成人讀的執行報告 report.md(零 token)")

    init_p = sub.add_parser("init", help="在專案生成 .agents 骨架與 AGENTS.md")
    init_p.add_argument("--path", default=".", metavar="DIR",
                        help="目標專案根目錄(預設:目前目錄)")

    for p in (run_p, status_p, check_p, report_p):
        p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=f"設定檔位置(預設:{_DEFAULT_CONFIG})")
    return parser


def _dispatch(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "init":
        return run_init(args.path)

    try:
        cfg = load_config(args.config)
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
