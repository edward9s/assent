"""CLI entry point: argparse subcommands run/status/check/report/clean/
reject/rework/init."""
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
from agents.rework import rework_task
from agents.terminal_log import terminal_logging

_DEFAULT_CONFIG = ".agents/agents.toml"


def _positive_int(value: str) -> int:
    """Parse a command-line integer that must be greater than zero."""
    try:
        number = int(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError("must be an integer") from e
    if number < 1:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents",
        description="An AI plan format plus an automatic scheduler: reads "
                    ".agents work folders, opens an AI session per task, "
                    "checks acceptance objectively, and auto-checkpoints git.",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{run,status,check,report,clean,reject,rework,init}")

    run_p = sub.add_parser(
        "run", help="Run the tasks in the given [FOLDER] until all are "
                    "DONE/BLOCKED/SKIP")
    run_p.add_argument("--once", action="store_true",
                       help="Run only the next task, then stop")
    run_p.add_argument("--task", metavar="ID",
                       help="Run one specific task (prerequisites still checked)")
    run_p.add_argument("--all", action="store_true", dest="all_folders",
                       help="Run all unfinished work folders in folder-dependency order")
    run_p.add_argument("--jobs", type=_positive_int, metavar="N",
                       help="Max folders to run concurrently with --all (default: 1)")

    status_p = sub.add_parser(
        "status", help="Show progress counts and the next task for the given "
                       "[FOLDER] (zero tokens)")
    check_p = sub.add_parser(
        "check", help="Validate the given [FOLDER]'s task-file format, config, "
                      "and environment (zero tokens; the meeting's exit gate)")
    report_p = sub.add_parser(
        "report", help="Generate the human-readable run report _report.md "
                       "(zero tokens)")
    clean_p = sub.add_parser(
        "clean", help="Remove worktrees and merged branches that are provably redundant")

    reject_p = sub.add_parser(
        "reject", help="Human ruling: reject a folder by archiving it, force-"
                       "removing its worktree and branch, and resetting its "
                       "tasks to TODO")
    reject_p.add_argument("folder", metavar="FOLDER",
                          help="The work folder to reject (required; cannot "
                               "target all folders)")
    reject_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                          help=f"Config file location (default: {_DEFAULT_CONFIG})")

    rework_p = sub.add_parser(
        "rework", help="Human ruling: reopen a single task, keeping its code "
                       "by default and not auto-running",
        description=(
            "Keeps code by default and only resets the given task to TODO; "
            "--cascade explicitly cascades to downstream tasks. --revert-code "
            "reverts code with a new commit, but only when the checkpoints "
            "form a contiguous branch tail. The command only updates status "
            "and reports; it does not auto-run."))
    rework_p.add_argument("folder", metavar="FOLDER",
                          help="The work folder containing the target task (required)")
    rework_p.add_argument("task", metavar="TASK",
                          help="The exact task id to reopen, e.g. t003 (required)")
    rework_p.add_argument(
        "--cascade", action="store_true",
        help="Explicitly also reset already-started or already-finished "
             "downstream tasks to TODO")
    rework_p.add_argument(
        "--revert-code", action="store_true",
        help="Revert code with a new commit, but only when the checkpoints "
             "form a contiguous branch tail")
    rework_p.add_argument("--reason", default="", metavar="TEXT",
                          help="The human ruling's reason, written to the rework log")
    rework_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                          help=f"Config file location (default: {_DEFAULT_CONFIG})")

    init_p = sub.add_parser(
        "init", help="Generate the .agents skeleton and AGENTS.md in a project")
    init_p.add_argument("--path", default=".", metavar="DIR",
                        help="Target project root directory (default: current directory)")

    for p in (run_p, status_p, check_p, report_p, clean_p):
        p.add_argument(
            "folder", nargs="?", metavar="FOLDER",
            help="The work folder; run derives it automatically if omitted, "
                 "other commands act on all folders")
        p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=f"Config file location (default: {_DEFAULT_CONFIG})")
    return parser


def _status_summary(plan: Plan) -> str:
    counts = Counter(task.status for task in plan.tasks)
    return (f"DONE {counts.get('DONE', 0)} / "
            f"BLOCKED {counts.get('BLOCKED', 0)} / "
            f"SKIP {counts.get('SKIP', 0)} / "
            f"WIP {counts.get('WIP', 0)} / "
            f"TODO {counts.get('TODO', 0)} ({len(plan.tasks)} total)")


def _select_run_folder(config_path: str, folders: list[str]) -> str | None:
    """Pick the one runnable folder from task and prerequisite status; any
    ambiguity or bad file is refused rather than guessed."""
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
        print(f"Work folder: {selected} (the only ongoing and runnable one, "
              f"selected automatically)")
        return selected

    print(f"Cannot auto-select a work folder: {len(runnable)} ongoing and "
          f"runnable folder(s) found.")
    print("Work folder status:")
    if not plans and not errors:
        print("  (no work folder with a task file found)")
    for folder, plan, waiting in plans:
        reason = f" (waiting on {', '.join(waiting)})" if waiting and any(
            task.status in ("TODO", "WIP") for task in plan.tasks) else ""
        print(f"  {folder}: {_status_summary(plan)}{reason}")
    for folder, error in errors:
        print(f"  {folder}: cannot be parsed ({error})")
    print("State the work folder explicitly: agents run <folder>")
    return None


def _dispatch_all(command: str, config_path: str, folders: list[str]) -> int:
    """Run a read-only command against every work folder in turn, aggregating
    the exit code."""
    if not folders:
        print("No work folder with a task file found.")
        return 1
    operation = getattr(engine, command)
    result = 0
    for index, folder in enumerate(folders):
        if index:
            print()
        try:
            cfg = load_config(config_path, folder)
        except AgentsError as e:
            print(f"Config error: {e}")
            result = 1
            continue
        if operation(cfg) != 0:
            result = 1
    return result


def _dispatch_check_all(config_path: str, agents_dir, folders: list[str]) -> int:
    """Validate every folder itself, plus the complete dependency graph and
    check for cycles."""
    graph_ok = True
    try:
        graph = parse_folder_dependency_graph(agents_dir)
        print(f"Folder dependency graph: OK ({len(graph)} work folder(s), "
              f"references complete and acyclic)")
    except AgentsError as e:
        graph_ok = False
        print(f"Folder dependency graph: FAIL ({e})")
    checks_ok = _dispatch_all("check", config_path, folders) == 0
    return 0 if graph_ok and checks_ok else 1


def _dispatch(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.all_folders and args.folder is not None:
            parser.error("run's --all and FOLDER cannot be used together")
        if args.all_folders and (args.once or args.task is not None):
            parser.error("run's --all cannot be used with --once or --task")
        if not args.all_folders and args.jobs is not None:
            parser.error("run's --jobs can only be used with --all")

    if args.command == "init":
        return run_init(args.path)

    try:
        agents_dir = validate_config(args.config)
    except AgentsError as e:
        print(f"Config error: {e}")
        return 1

    if args.command == "run" and args.all_folders:
        return run_all(args.config, agents_dir, args.jobs or 1)
    if args.command == "reject":
        try:
            cfg = load_config(args.config, args.folder)
        except AgentsError as e:
            print(f"Config error: {e}")
            return 1
        return reject_folder(cfg)
    if args.command == "rework":
        try:
            cfg = load_config(args.config, args.folder)
        except AgentsError as e:
            print(f"Config error: {e}")
            return 1
        return rework_task(
            cfg, args.task, cascade=args.cascade,
            reason=args.reason, revert_code=args.revert_code)
    folders = list_task_folders(agents_dir)
    if args.command == "clean":
        selected = folders if args.folder is None else [args.folder]
        if not selected:
            print("No work folder with a task file found.")
            return 1
        configs = []
        for selected_folder in selected:
            try:
                configs.append(load_config(args.config, selected_folder))
            except AgentsError as e:
                print(f"Config error: {e}")
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
        print(f"Config error: {e}")
        return 1

    if args.command == "run":
        return engine.run(cfg, once=args.once, task_id=args.task)
    if args.command == "status":
        return engine.status(cfg)
    if args.command == "check":
        return engine.check(cfg)
    if args.command == "report":
        return engine.report(cfg)
    return 2  # argparse required=True already guards this; defensive fallback


def _install_break_handler() -> None:
    """Windows-only: turn CTRL_BREAK_EVENT into KeyboardInterrupt.

    ``run --all`` starts its child process with CREATE_NEW_PROCESS_GROUP, so an
    interrupt can only be sent as CTRL_BREAK_EVENT (mapped to SIGBREAK). If the
    child has not registered a handler, the OS terminates it directly on
    receiving the signal (exit code 3221225786), and engine's interrupt
    cleanup (WIP marking, the r-file interrupt entry, the wip checkpoint)
    never runs at all -- violating "token-burned output is never discarded".
    Rebinding to default_int_handler makes SIGBREAK take the same
    KeyboardInterrupt path as Ctrl+C. POSIX has no SIGBREAK, so behavior there
    is unchanged.
    """
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)


def main(argv: list[str] | None = None) -> int:
    _install_break_handler()
    # On Windows, stdout/stderr default to the system code page when
    # redirected to a pipe/file, which mangles non-ASCII output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    with terminal_logging(actual_argv):
        return _dispatch(actual_argv)


if __name__ == "__main__":
    sys.exit(main())
