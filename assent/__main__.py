"""CLI entry point: argparse subcommands run/status/check/report/verify/clean/
accept/reconcile/reject/rework/archive/init."""
from __future__ import annotations

import _thread
import argparse
import importlib.metadata
import os
import signal
import sys
import threading
from collections import Counter

from assent import AssentError, contracts, engine, inspection
from assent.accept import accept_folder
from assent.archive import archive_all, archive_folder, restore_folder
from assent.batch_accept import accept_all, accept_selected_batch
from assent.clean import clean_folders
from assent.config import list_task_folders, load_config, validate_config
from assent.doctor import doctor as run_doctor
from assent.folderdeps import (find_unfinished_prerequisites,
                               parse_folder_dependency_graph)
from assent.folder_scheduler import run_all
from assent.init import init as run_init
from assent.plan import Plan
from assent.reconcile import (reconcile_abort, reconcile_continue,
                              reconcile_start)
from assent.reject import reject_folder
from assent.rework import rework_task
from assent.terminal_log import terminal_logging
from assent.verification import (verify_batch, verify_folder,
                                  verify_selected_batch)

_DEFAULT_CONFIG = ".assent/assent.toml"
# The project settings file is optional: it layers over the user-wide settings and
# locates the project whether or not it exists, so its absence is not an error.
_CONFIG_HELP = (
    "Optional project settings file, layered over the user-wide "
    "~/.assent/assent.toml, and the locator of the project's .assent directory "
    f"(default: {_DEFAULT_CONFIG})")
# Set by the parent scheduler on a spawned `assent run <folder>` child to opt
# that child into the stdin stop channel; a hand-typed `assent run` never sees
# it, so an interactive stdin (possibly a tty) is left completely alone.
_STDIN_STOP_ENV = "ASSENT_STDIN_STOP"


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
        prog="assent",
        description="An AI plan format plus an automatic scheduler: reads "
                    ".assent work folders, opens an AI session per task, "
                    "checks acceptance objectively, and auto-checkpoints git.",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {importlib.metadata.version('assent')}",
        help="Show the installed assent distribution version and exit",
    )
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="{run,status,check,report,verify,clean,accept,reconcile,reject,rework,archive,init,doctor}")

    run_p = sub.add_parser(
        "run", help="Run one or more folders in order until all are "
                    "DONE/BLOCKED/SKIP")
    run_p.add_argument(
        "folders", nargs="*", metavar="FOLDER",
        help="Work folders to run in the stated order; omit to select one "
             "automatically")
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
    verify_p = sub.add_parser(
        "verify", help="Refresh full integration verification for one folder, "
                       "an exact selected batch, or every queued folder with "
                       "--batch")
    verify_p.add_argument(
        "folder", nargs="*", metavar="FOLDER",
        help="One completed folder, or two or more exact folders to verify "
             "as one dependency-ordered candidate (omit with --batch)")
    verify_p.add_argument(
        "--batch", action="store_true",
        help="Merge every finished, not-yet-integrated folder in folder-"
             "dependency order into one candidate and verify it once; a "
             "conflicting source is reported and, after one confirmation, "
             "skipped together with the folders queued after it")
    verify_p.add_argument(
        "--no-bisect", action="store_false", dest="bisect",
        help="With --batch or an exact selected batch, record a failure as-is "
             "instead of localizing it to the first folder that breaks the batch")
    verify_p.add_argument(
        "--focus", action="store_true",
        help="With exactly one FOLDER, rerun its distinct DONE-task focused "
             "verify commands in the source worktree; this cannot authorize "
             "accept and creates no receipt")
    verify_p.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=_CONFIG_HELP)
    clean_p = sub.add_parser(
        "clean", help="Remove worktrees and merged branches that are provably redundant")

    archive_p = sub.add_parser(
        "archive", help="Retire a finished folder: clean it, then compress its plan "
                        "into _archive/ and register it in the roster; --restore "
                        "reverses one archive")
    archive_p.add_argument(
        "folder", nargs="?", metavar="FOLDER",
        help="The finished work folder to archive or restore (omit only with --all)")
    archive_p.add_argument(
        "--all", action="store_true", dest="all_folders",
        help="Archive every eligible finished folder in lexicographic order; "
             "ineligible folders are skipped, not failed")
    archive_p.add_argument(
        "--restore", action="store_true",
        help="Reverse one archive: extract the zip back to the live folder, "
             "deregister it, and delete the zip (cannot be combined with --all)")
    archive_p.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=_CONFIG_HELP)

    accept_p = sub.add_parser(
        "accept", help="Transactionally integrate one reviewed, finished folder "
                       "into the main worktree's current branch, an exact "
                       "selected batch, or every finished folder with --all")
    accept_p.add_argument(
        "folder", nargs="*", metavar="FOLDER",
        help="One reviewed work folder, or two or more exact folders to accept "
             "as a verified batch (omit only with --all)")
    accept_p.add_argument(
        "--all", action="store_true", dest="all_folders",
        help="Accept every finished work folder in folder-dependency order, "
             "refreshing any stale verification receipt first (sequential only)")
    accept_p.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=_CONFIG_HELP)

    reconcile_p = sub.add_parser(
        "reconcile", help="Resolve one folder's source-versus-target conflict "
                          "by hand in an isolated worktree; runs no "
                          "verification and integrates nothing",
        description=(
            "Prepares the conflict in a dedicated worktree, and with "
            "--continue turns the human's resolution into a merge commit the "
            "folder's own source branch is fast-forwarded onto. It never "
            "touches the integration target, never runs the focused or the "
            "complete verification, and never accepts: `assent verify FOLDER` "
            "and then `assent accept FOLDER` stay separate, explicit steps."))
    reconcile_p.add_argument(
        "folder", metavar="FOLDER",
        help="The finished work folder to reconcile (required; one folder "
             "only, never a speculative set of peers)")
    reconcile_action = reconcile_p.add_mutually_exclusive_group()
    reconcile_action.add_argument(
        "--continue", action="store_true", dest="continue_reconcile",
        help="Finish the reconciliation started earlier: stage the resolved "
             "conflict, commit the merge, and fast-forward the source branch")
    reconcile_action.add_argument(
        "--abort", action="store_true",
        help="Discard the reconciliation attempt; the source and the "
             "integration target are left unchanged")
    reconcile_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                             help=_CONFIG_HELP)

    reject_p = sub.add_parser(
        "reject", help="Human ruling: reject a folder by archiving it, force-"
                       "removing its worktree and branch, and resetting its "
                       "tasks to TODO")
    reject_p.add_argument("folder", metavar="FOLDER",
                          help="The work folder to reject (required; cannot "
                               "target all folders)")
    reject_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                          help=_CONFIG_HELP)

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
                          help=_CONFIG_HELP)

    init_p = sub.add_parser(
        "init", help="Generate or upgrade the .assent skeleton and managed project files")
    init_p.add_argument("--path", default=".", metavar="DIR",
                        help="Target project root directory (default: current directory)")
    init_p.add_argument(
        "--test", nargs="+", metavar="CHOICE",
        help=("Select the project test non-interactively: 1/unittest, 2/pytest, "
              "3/npm, 4/flutter, or 5/custom followed by an argv command; "
              "custom:<command> also accepts one quoted command. Omit it on "
              "fresh init for the numbered menu; repeat init does not prompt"))

    sub.add_parser(
        "doctor", help="Diagnose the machine environment (Python, git, "
                       "adapter CLIs, temp directory); needs no existing "
                       ".assent/ project")

    for p in (status_p, check_p, report_p, clean_p):
        p.add_argument(
            "folder", nargs="?", metavar="FOLDER",
            help="The work folder; omit to act on all folders")
        p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=_CONFIG_HELP)
    # ``run`` has its own ordered positional list and still accepts the same
    # config option as the other plan commands.
    run_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=_CONFIG_HELP)
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
        except AssentError as e:
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
    print("State the work folder explicitly: assent run <folder>")
    return None


def _dispatch_all(command: str, config_path: str, folders: list[str]) -> int:
    """Run a read-only command against every work folder in turn, aggregating
    the exit code."""
    if not folders:
        print("No work folder with a task file found.")
        return 1
    operation = getattr(inspection, command)
    result = 0
    for index, folder in enumerate(folders):
        if index:
            print()
        try:
            cfg = load_config(config_path, folder)
        except AssentError as e:
            print(f"Config error: {e}")
            result = 1
            continue
        if operation(cfg) != 0:
            result = 1
    return result


def _dispatch_check_all(config_path: str, assent_dir, folders: list[str]) -> int:
    """Validate every folder itself, plus the complete dependency graph and
    check for cycles."""
    graph_ok = True
    try:
        graph = parse_folder_dependency_graph(assent_dir)
        print(f"Folder dependency graph: OK ({len(graph)} work folder(s), "
              f"references complete and acyclic)")
    except AssentError as e:
        graph_ok = False
        print(f"Folder dependency graph: FAIL ({e})")
    checks_ok = _dispatch_all("check", config_path, folders) == 0
    return 0 if graph_ok and checks_ok else 1


def _dispatch_run_folders(
        config_path: str, folders: list[str], *, once: bool,
        task_id: str | None) -> int:
    """Run explicitly named folders in order, stopping on the first failure."""
    for folder in folders:
        try:
            cfg = load_config(config_path, folder)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        result = engine.run(cfg, once=once, task_id=task_id)
        if result != 0:
            return result
    return 0


def _dispatch(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        if len(args.folders) != len(set(args.folders)):
            parser.error("run does not allow duplicate FOLDER names")
        if args.all_folders and (args.once or args.task is not None):
            parser.error("run's --all cannot be used with --once or --task")
        if len(args.folders) > 1 and (args.once or args.task is not None):
            parser.error("run's --once and --task each require at most one FOLDER")
        if not args.all_folders and args.jobs is not None:
            parser.error("run's --jobs can only be used with --all")

    if args.command == "accept":
        if args.all_folders and args.folder:
            parser.error("accept's --all and FOLDER cannot be used together")
        if not args.all_folders and not args.folder:
            parser.error("accept requires FOLDER or --all")
        if len(args.folder) > 1 and len(args.folder) != len(set(args.folder)):
            parser.error("accept does not allow duplicate FOLDER names")

    if args.command == "verify":
        if args.batch and args.folder:
            parser.error("verify's --batch and FOLDER cannot be used together")
        if args.focus:
            if args.batch:
                parser.error("verify's --focus and --batch cannot be used together")
            if len(args.folder) != 1:
                parser.error("verify's --focus requires exactly one FOLDER")
            if not args.bisect:
                parser.error("verify's --no-bisect cannot be used with --focus")
        elif not args.batch:
            if not args.folder:
                parser.error("verify requires FOLDER, a selected batch, or --batch")
            if len(args.folder) == 1 and not args.bisect:
                parser.error("verify's --no-bisect only applies to a batch")
            if len(args.folder) > 1 and len(args.folder) != len(set(args.folder)):
                parser.error("verify does not allow duplicate FOLDER names")

    if args.command == "archive":
        if args.restore:
            if args.all_folders:
                parser.error("archive's --restore and --all cannot be used together")
            if args.folder is None:
                parser.error("archive --restore requires FOLDER")
        elif args.all_folders and args.folder is not None:
            parser.error("archive's --all and FOLDER cannot be used together")
        elif not args.all_folders and args.folder is None:
            parser.error("archive requires FOLDER or --all")

    if args.command == "init":
        return run_init(args.path, args.test)

    if args.command == "doctor":
        return run_doctor()

    try:
        assent_dir = validate_config(args.config)
    except AssentError as e:
        print(f"Config error: {e}")
        return 1

    if args.command == "run":
        # The session gate for the global contracts: a run that would point the
        # execution AI at a missing or out-of-date ~/.assent contract is refused
        # here, before any folder is opened and before any adapter process exists.
        try:
            contracts.require_contracts()
        except AssentError as e:
            print(f"Global contracts: FAIL ({e})")
            return 1
        if args.folders:
            result = _dispatch_run_folders(
                args.config, args.folders, once=args.once, task_id=args.task)
            if result != 0:
                return result
        if args.all_folders:
            return run_all(args.config, assent_dir, args.jobs or 1)
        if args.folders:
            return 0
    if args.command == "accept":
        if args.all_folders:
            return accept_all(args.config, assent_dir)
        if len(args.folder) >= 2:
            return accept_selected_batch(
                args.config, assent_dir, args.folder)
        try:
            cfg = load_config(args.config, args.folder[0])
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return accept_folder(cfg)
    if args.command == "archive":
        if args.all_folders:
            return archive_all(args.config, assent_dir)
        try:
            cfg = load_config(args.config, args.folder)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return restore_folder(cfg) if args.restore else archive_folder(cfg)
    if args.command == "verify":
        if not args.batch:
            try:
                cfg = load_config(args.config, args.folder[0])
            except AssentError as e:
                print(f"Config error: {e}")
                return 1
        try:
            if args.batch:
                return verify_batch(args.config, assent_dir, args.bisect)
            if args.focus:
                return engine.verify_focused(cfg)
            if len(args.folder) == 1:
                return verify_folder(cfg)
            return verify_selected_batch(
                args.config, assent_dir, args.folder, args.bisect)
        except KeyboardInterrupt:
            print("\nverify interrupted; temporary resources were cleaned up.")
            return 130
    if args.command == "reconcile":
        try:
            cfg = load_config(args.config, args.folder)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        if args.continue_reconcile:
            return reconcile_continue(cfg)
        if args.abort:
            return reconcile_abort(cfg)
        return reconcile_start(cfg)
    if args.command == "reject":
        try:
            cfg = load_config(args.config, args.folder)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return reject_folder(cfg)
    if args.command == "rework":
        try:
            cfg = load_config(args.config, args.folder)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return rework_task(
            cfg, args.task, cascade=args.cascade,
            reason=args.reason, revert_code=args.revert_code)
    folders = list_task_folders(assent_dir)
    if args.command == "clean":
        selected = folders if args.folder is None else [args.folder]
        if not selected:
            print("No work folder with a task file found.")
            return 1
        configs = []
        for selected_folder in selected:
            try:
                configs.append(load_config(args.config, selected_folder))
            except AssentError as e:
                print(f"Config error: {e}")
                return 1
        return clean_folders(configs)
    if args.command == "run":
        folder = _select_run_folder(args.config, folders)
        if folder is None:
            return 1
    elif args.folder is None:
        if args.command == "check":
            return _dispatch_check_all(args.config, assent_dir, folders)
        else:
            return _dispatch_all(args.command, args.config, folders)
    else:
        folder = args.folder

    try:
        cfg = load_config(args.config, folder)
    except AssentError as e:
        print(f"Config error: {e}")
        return 1

    if args.command == "run":
        return engine.run(cfg, once=args.once, task_id=args.task)
    if args.command == "status":
        return inspection.status(cfg)
    if args.command == "check":
        return inspection.check(cfg)
    if args.command == "report":
        return inspection.report(cfg)
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


def _start_stdin_stop_watcher() -> threading.Thread | None:
    """Opt-in stop channel: treat the parent closing our stdin as Ctrl+C.

    ``run --all`` cannot rely on console signals to stop a child. Under tmux or
    mintty the child's pty is not a Win32 console, so
    ``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT)`` never reaches it and the
    parent waits forever. A stdin pipe is platform-independent and always
    reaches the child, so the parent closes it (or dies, which closes it too)
    and this daemon thread turns the resulting EOF -- or any byte -- into
    ``interrupt_main()``. That raises KeyboardInterrupt in the main thread, so
    the existing interrupt cleanup runs unchanged: WIP marking, the r-file
    interrupt entry, the wip checkpoint, exit 130. As a side effect a child
    whose parent crashes also cleans itself up instead of becoming an orphan.

    Without ``ASSENT_STDIN_STOP`` no thread is started at all, so a manual
    ``assent run`` keeps its stdin untouched.
    """
    if not os.environ.get(_STDIN_STOP_ENV):
        return None
    stream = getattr(sys.stdin, "buffer", sys.stdin)
    if stream is None:
        return None

    # The scheduler's pipe belongs only to this watcher.  If fd 0 is left
    # attached to it, subprocesses started by the run inherit the same pipe.
    # On Windows a blocking read in this thread can then keep even a simple
    # captured Git command from reaching EOF.  Retain a private, non-inherited
    # duplicate for stop requests and make descendants' stdin non-interactive.
    watcher_stream = stream
    owns_watcher_stream = False
    try:
        stream_fd = stream.fileno()
        watcher_fd = os.dup(stream_fd)
        try:
            with open(os.devnull, "rb", buffering=0) as devnull:
                os.dup2(devnull.fileno(), stream_fd, inheritable=True)
            watcher_stream = os.fdopen(watcher_fd, "rb", buffering=0)
            owns_watcher_stream = True
        except (OSError, ValueError):
            os.close(watcher_fd)
            raise
    except (AttributeError, OSError, ValueError):
        # Embedded/test streams without a real file descriptor still retain
        # the original stop behavior; they cannot leak an OS pipe to a child.
        watcher_stream = stream

    def watch() -> None:
        try:
            watcher_stream.read(1)
        except (OSError, ValueError):
            pass  # stdin torn down under us -- still a stop request
        finally:
            if owns_watcher_stream:
                watcher_stream.close()
        _thread.interrupt_main()

    thread = threading.Thread(target=watch, name="assent-stdin-stop", daemon=True)
    thread.start()
    return thread


def main(argv: list[str] | None = None) -> int:
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    _install_break_handler()
    # The stop channel belongs to a real scheduler-spawned ``run`` process.
    # ``main(argv)`` is also the in-process CLI entry point used by tests and
    # library callers; starting a watcher there would let the caller's closed
    # stdin interrupt unrelated dispatch.  Help should likewise remain a
    # normal parser operation even when it inherits the scheduler environment.
    if (argv is None and actual_argv[:1] == ["run"]
            and "-h" not in actual_argv and "--help" not in actual_argv):
        _start_stdin_stop_watcher()
    # On Windows, stdout/stderr default to the system code page when
    # redirected to a pipe/file, which mangles non-ASCII output.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    with terminal_logging(actual_argv):
        return _dispatch(actual_argv)


if __name__ == "__main__":
    sys.exit(main())
