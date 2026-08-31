"""CLI entry point: argparse subcommands run/test/status/check/report/verify/clean/
accept/reconcile/reject/rework/archive/init."""
from __future__ import annotations

import _thread
import argparse
import dataclasses
import functools
import importlib.metadata
import os
import signal
import sys
import threading
import time
from pathlib import Path

from assent import AssentError, engine, gitops, inspection
from assent.accept import accept_plan
from assent.adapters.process import wake_stop_waiters
from assent.archive import (archive_all, archive_plan, archive_recovery_names,
                            archive_selected, restore_plan)
from assent.batch_accept import accept_all, accept_selected_batch
from assent.clean import clean_plans, validate_live_plan_selection
from assent.config import (list_task_plans, load_config,
                           load_main_runtime_config, validate_config)
from assent.doctor import doctor as run_doctor
from assent.plandeps import infer_plan_completion, parse_plan_dependency_graph
from assent.plan_source import resolve_source_snapshot
from assent.plan_scheduler import run_all
from assent.init import init as run_init
from assent.ignored_dirs_cli import (add_ignored_dirs_command,
                                     ignored_dirs_declare,
                                     ignored_dirs_status)
from assent.plan import (plan_workflow_requires_human,
                         read_runtime_test_workflow_state)
from assent.reconcile import (reconcile_abort, reconcile_continue,
                              reconcile_start)
from assent.reject import reject_plan
from assent.rework import rework_task
from assent.terminal_log import terminal_logging
from assent.verification import (verify_batch, verify_plan,
                                  verify_plan_if_needed,
                                  verify_selected_batch)

_DEFAULT_CONFIG = ".assent/assent.toml"
# The project settings file is optional: it layers over the user-wide settings and
# locates the project whether or not it exists, so its absence is not an error.
_CONFIG_HELP = (
    "Optional project settings file, layered over the user-wide "
    "~/.assent/assent.toml, and the locator of the project's .assent directory "
    f"(default: {_DEFAULT_CONFIG})")
# Set by the parent scheduler on a spawned `assent run <plan>` child to opt
# that child into the stdin stop channel; a hand-typed `assent run` never sees
# it, so an interactive stdin (possibly a tty) is left completely alone.
_STDIN_STOP_ENV = "ASSENT_STDIN_STOP"
# Also set by the parent scheduler on a spawned `assent run <plan>` child.  The
# end-to-end total belongs to the user's own invocation, so a child reports its
# plan duration under its own label instead of a second, identical-looking
# command total.
_PLAN_CHILD_ENV = "ASSENT_PLAN_RUN"
# The two long-running commands whose wall-clock duration is worth reporting:
# they open AI sessions, build integration candidates, and run whole suites.
# Every other subcommand returns promptly and its output stays untouched.
_TIMED_COMMANDS = ("run", "verify")
# Named so tests can inject a deterministic clock; production always reads the
# monotonic clock, which no wall-clock or timezone change can move backwards.
_monotonic = time.monotonic
_PLAN_NAME_HELP = (" Each PLAN names a directory directly under the project's "
                   "`.assent/` (for example, `demo` means `.assent/demo/`); "
                   "pass the name, not a path.")


class _HelpFormatter(argparse.HelpFormatter):
    """The standard help formatter with a readable heading color.

    Python 3.14 colorizes argparse help by default and paints the ``usage:``
    prefix and the section headings dark blue (``1;34``), which is barely
    legible on a dark terminal.  Only those two theme fields are swapped for
    bright cyan (``1;96``); every option, label and action color stays exactly
    as the standard theme defines it.  The swap happens inside argparse's own
    ``_set_color``, so all of its terminal and environment checks (``NO_COLOR``,
    ``FORCE_COLOR``, ``PYTHON_COLORS``, a redirected or unsupported stream) still
    decide whether any escape is emitted at all: when color is off the theme is
    the all-empty-string one, whose empty ``reset`` is what this checks before
    substituting anything.  Python 3.11-3.13 argparse has neither ``_set_color``
    nor a ``color`` argument, so there the subclass is an ordinary formatter and
    help stays plain.
    """

    _HEADING_COLOR = "\x1b[1;96m"

    def _set_color(self, color: bool) -> None:
        super()._set_color(color)
        if self._theme.reset:
            self._theme = dataclasses.replace(
                self._theme, usage=self._HEADING_COLOR,
                heading=self._HEADING_COLOR)


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
                    "plans from the project's .assent directory, opens an AI "
                    "session per task, "
                    "checks acceptance objectively, and auto-checkpoints git.",
        formatter_class=_HelpFormatter,
    )
    parser.add_argument(
        "--version", action="version",
        version=f"%(prog)s {importlib.metadata.version('assent')}",
        help="Show the installed assent distribution version and exit",
    )
    # add_parser passes prog and the color decision down to a subparser but not
    # the formatter class, so the shared palette is installed once by fixing the
    # class every subparser is constructed from.
    sub = parser.add_subparsers(
        dest="command", required=True,
        parser_class=functools.partial(argparse.ArgumentParser,
                                       formatter_class=_HelpFormatter),
        metavar=("{run,test,status,check,report,verify,clean,accept,reconcile,reject,"
                 "rework,archive,init,doctor,ignored-dirs}"))

    run_p = sub.add_parser(
        "run", help="Run every discovered plan, or the exact named plans, "
                    "through task, plan, and integration workflows")
    run_p.add_argument(
        "plan_names", nargs="*", metavar="PLAN",
        help="Exact plans to run in the stated order; omit to schedule every "
             "discovered plan in dependency order." + _PLAN_NAME_HELP)
    run_p.add_argument("--jobs", type=_positive_int, metavar="N",
                       help="Maximum concurrent plans for a whole-project run "
                            "(default: 1; cannot be used with PLAN)")

    test_p = sub.add_parser(
        "test", help="Test one live plan, or repair-test the current main",
        description=(
            "With PLAN, execute that exact live plan's declared runtime command "
            "in its candidate worktree. Without PLAN, execute the project-layer "
            "runtime command in the current primary working tree. This command "
            "never dispatches task, plan, integration, full verification, or "
            "accept workflows. Startup output names the target, command source, "
            "working tree, and current workflow step."))
    test_p.add_argument(
        "plan_name", nargs="?", metavar="PLAN",
        help="Exact live plan for the plan runtime workflow; omit PLAN to test "
             "the current main candidate." + _PLAN_NAME_HELP)
    test_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                        help=_CONFIG_HELP)

    status_p = sub.add_parser(
        "status", help="Show progress counts and the next task for the given "
                       "[PLAN] (zero tokens)")
    check_p = sub.add_parser(
        "check", help="Validate the given [PLAN]'s task-file format, config, "
                      "and environment (zero tokens; the meeting's exit gate)")
    report_p = sub.add_parser(
        "report", help="Generate the human-readable run report _report.md "
                       "(zero tokens)")
    verify_p = sub.add_parser(
        "verify", help="Run a requested mechanical verification without AI "
                       "review or automatic repair")
    verify_p.add_argument(
        "plan_name", nargs="*", metavar="PLAN",
        help="One plan with --focus; otherwise one completed plan or two or "
             "more exact plans to verify as one dependency-ordered candidate "
             "(omit with --batch)." + _PLAN_NAME_HELP)
    verify_p.add_argument(
        "--batch", action="store_true",
        help="Merge every finished, not-yet-integrated plan in "
             "dependency order into one candidate and verify it once; a "
             "conflicting source is reported and, after one confirmation, "
             "skipped together with the plans queued after it")
    verify_p.add_argument(
        "--focus", nargs="?", const="", metavar="TASK",
        help="With exactly one PLAN, run the named task's focused verify "
             "command; omit TASK to sweep the distinct verify commands of "
             "all DONE tasks. Focused verification cannot authorize accept "
             "and creates no receipt")
    verify_p.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=_CONFIG_HELP)
    clean_p = sub.add_parser(
        "clean", help="Remove worktrees and merged branches that are provably redundant")
    clean_p.add_argument(
        "plan_name", nargs="*", metavar="PLAN",
        help="The plans to clean upstream-first; omit to act on all plans."
             + _PLAN_NAME_HELP)
    clean_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                         help=_CONFIG_HELP)

    archive_p = sub.add_parser(
        "archive", help="Retire a finished plan: clean it, then compress its records "
                        "into _archive/ and register it in the roster; --restore "
                        "reverses one archive")
    archive_p.add_argument(
        "plan_name", nargs="*", metavar="PLAN",
        help="The finished plans to archive, or the one plan to restore "
             "(omit only with --all)." + _PLAN_NAME_HELP)
    archive_p.add_argument(
        "--all", action="store_true", dest="all_plans",
        help="Archive every eligible finished plan in lexicographic order; "
             "ineligible plans are skipped, not failed")
    archive_p.add_argument(
        "--restore", action="store_true",
        help="Reverse one archive: extract the zip back to the live plan, "
             "deregister it, and delete the zip (cannot be combined with --all)")
    archive_p.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=_CONFIG_HELP)

    accept_p = sub.add_parser(
        "accept", help="Transactionally integrate one reviewed, finished plan "
                       "into the main worktree's current branch, an exact "
                       "selected batch, or every finished plan with --all")
    accept_p.add_argument(
        "plan_name", nargs="*", metavar="PLAN",
        help="One reviewed plan, or two or more exact plans to accept "
             "as a verified batch (omit only with --all)." + _PLAN_NAME_HELP)
    accept_p.add_argument(
        "--all", action="store_true", dest="all_plans",
        help="Accept every finished plan in dependency order: a "
             "fresh PASSED batch receipt is replayed and released atomically "
             "without new verification, while absent or expired batch evidence "
             "verifies each not-yet-integrated plan in turn, stops at the "
             "first failure, and keeps the plans already published")
    accept_p.add_argument(
        "--config", default=_DEFAULT_CONFIG, metavar="PATH",
        help=_CONFIG_HELP)

    reconcile_p = sub.add_parser(
        "reconcile", help="Resolve one plan's source-versus-target conflict "
                          "by hand in an isolated worktree; runs no "
                          "verification and integrates nothing",
        description=(
            "Prepares the conflict in a dedicated worktree, and with "
            "--continue turns the human's resolution into a merge commit the "
            "plan's own source branch is fast-forwarded onto. It never "
            "touches the integration target, never runs the focused or the "
            "complete verification, and never accepts: `assent verify PLAN` "
            "and then `assent accept PLAN` stay separate, explicit steps."))
    reconcile_p.add_argument(
        "plan_name", metavar="PLAN",
        help="The finished plan to reconcile (required; one plan only, never "
             "a speculative set of peers)." + _PLAN_NAME_HELP)
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
        "reject", help="Human ruling: reject a plan by archiving it, force-"
                       "removing its worktree and branch, and resetting its "
                       "tasks to TODO")
    reject_p.add_argument("plan_name", metavar="PLAN",
                          help="The plan to reject (required; cannot target all "
                               "plans)." + _PLAN_NAME_HELP)
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
    rework_p.add_argument("plan_name", metavar="PLAN",
                          help="The plan containing the target task (required)."
                               + _PLAN_NAME_HELP)
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
        "init", help="Install or refresh the shared ~/.assent settings and "
                     "contracts, and generate or maintain this project's "
                     ".assent skeleton")
    init_p.add_argument("--path", default=".", metavar="DIR",
                        help="Target project root directory (default: current directory)")
    init_p.add_argument(
        "--test", nargs="+", metavar="CHOICE",
        help=("Select the project test non-interactively: 0/custom followed "
              "by an argv command (custom:<command> also accepts one quoted "
              "command), 1/unittest, 2/pytest, 3/npm, 4/flutter, 5/dotnet, "
              "6/maven, 7/gradle, 8/cmake-ctest, or 9/make. Omit it for the "
              "numbered menu when creating a verifier or after agreeing to "
              "replace one; an explicit repeat choice backs up and replaces "
              "a differing verifier"))

    # The only sanctioned writer of the local ignored-directory manifest.  It needs no
    # .assent project config: it acts on the Git worktree it is run in.
    add_ignored_dirs_command(sub)

    sub.add_parser(
        "doctor", help="Diagnose the machine environment (Python, git, "
                       "adapter CLIs, temp directory); needs no existing "
                       ".assent/ project")

    for p in (status_p, check_p, report_p):
        p.add_argument(
            "plan_name", nargs="?", metavar="PLAN",
            help="The plan; omit to act on all plans." + _PLAN_NAME_HELP)
        p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=_CONFIG_HELP)
    # ``run`` has its own ordered positional list and still accepts the same
    # config option as the other plan commands.
    run_p.add_argument("--config", default=_DEFAULT_CONFIG, metavar="PATH",
                       help=_CONFIG_HELP)
    return parser


def _runtime_workflow_step(cfg) -> str:
    """Describe the current runtime workflow cursor without changing it."""
    workflow = cfg.workflow_runtime_test
    if workflow is None:
        return "unconfigured"
    try:
        state = read_runtime_test_workflow_state(cfg.tasks_dir)
    except AssentError as error:
        return f"unavailable ({error})"
    index = state.step_index if state is not None else 0
    if index >= len(workflow):
        if state is not None and state.action_status != "PASSED":
            status = state.action_status or "unresolved"
            return f"{len(workflow)}/{len(workflow)}: {status}"
        return f"{len(workflow)}/{len(workflow)}: complete"
    step = workflow[index]
    label = getattr(step, "action", None) or getattr(step, "role", "role")
    return f"{index + 1}/{len(workflow)}: {label}"


def _print_runtime_test_start(cfg, *, plan_name: str | None) -> None:
    """Show the one target and source selected by ``assent test``."""
    if plan_name is None:
        target = "current main"
        command_source = ("project config [runtime_test].command ("
                          f"{cfg.assent_dir / 'assent.toml'})")
        candidate = cfg.root
    else:
        target = f"live plan {plan_name}"
        command_source = f"plan contract ({cfg.tasks_dir / '_runtime_test.toml'})"
        candidate = gitops.worktree_path(cfg.root, plan_name)
    print(f"Runtime test target: {target}")
    print(f"Runtime test command source: {command_source}")
    print(f"Runtime test working tree: {candidate}")
    print(f"Runtime test workflow step: {_runtime_workflow_step(cfg)}")


def _runtime_test_cli_exit_code(result: int) -> int:
    """Apply the runtime-test CLI contract to an engine result."""
    return result if result in (0, 130) else 1


def _run_plan_runtime_test(cfg) -> int:
    """Expose a failed or unresolved plan runtime state as a CLI failure."""
    result = _runtime_test_cli_exit_code(engine.run_runtime_test(cfg))
    if result != 0:
        return result
    try:
        state = read_runtime_test_workflow_state(cfg.tasks_dir)
    except AssentError as error:
        print(f"Runtime test failed: {error}")
        return 1
    if state is None or state.action_status != "PASSED":
        return 1
    return result


def _validate_explicit_plans(assent_dir: Path, plan_names: list[str], *,
                               recognized: list[str] | set[str] = ()) -> bool:
    """Run the shared identity gate for a non-empty explicit plan prefix."""
    return (not plan_names or validate_live_plan_selection(
        assent_dir, plan_names, recognized=recognized))


def _dispatch_all(command: str, config_path: str, plan_names: list[str]) -> int:
    """Run a read-only command against every plan in turn, aggregating
    the exit code."""
    if not plan_names:
        print("No plan with a task file found.")
        return 1
    operation = getattr(inspection, command)
    result = 0
    for index, plan_name in enumerate(plan_names):
        if index:
            print()
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError as e:
            print(f"Config error: {e}")
            result = 1
            continue
        if operation(cfg) != 0:
            result = 1
    return result


def _dispatch_check_all(config_path: str, assent_dir, plan_names: list[str]) -> int:
    """Validate every plan itself, plus the complete dependency graph and
    check for cycles."""
    graph_ok = True
    try:
        graph = parse_plan_dependency_graph(assent_dir)
        print(f"Plan dependency graph: OK ({len(graph)} plan(s), "
              f"references complete and acyclic)")
    except AssentError as e:
        graph_ok = False
        print(f"Plan dependency graph: FAIL ({e})")
    checks_ok = _dispatch_all("check", config_path, plan_names) == 0
    return 0 if graph_ok and checks_ok else 1


def _dispatch_run_plans(config_path: str, plan_names: list[str]) -> int:
    """Run explicitly named plans in order, stopping on the first failure."""
    for plan_name in plan_names:
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        result = engine.run(cfg)
        if result != 0:
            return result
    return 0


def _accepted_run_source(cfg) -> tuple[str, str, str] | None:
    """Return the current source and target when this plan is integrated."""
    try:
        if not infer_plan_completion(cfg.tasks_dir).complete:
            return None
        main = gitops.main_worktree(cfg.root)
        target_branch = gitops.require_current_branch(main)
        target_tip = gitops.commit_of(main, target_branch)
        source_branch, source_tip, _worktree = resolve_source_snapshot(
            main, cfg.tasks_name, cfg.git_excludes, operation="run")
    except AssentError:
        return None
    if not gitops.is_ancestor(main, source_tip, target_tip):
        return None
    return source_branch, source_tip, target_branch


def _filter_accepted_run_plans(
        config_path: str, plan_names: list[str]) -> list[str]:
    """Skip named plans whose complete current source is already integrated."""
    pending: list[str] = []
    for plan_name in plan_names:
        try:
            cfg = load_config(config_path, plan_name)
        except AssentError:
            pending.append(plan_name)
            continue
        accepted = _accepted_run_source(cfg)
        if accepted is None:
            pending.append(plan_name)
            continue
        source_branch, source_tip, target_branch = accepted
        print(f"run {plan_name}: already accepted; current source {source_branch} "
              f"({source_tip[:12]}) is fully contained in {target_branch}. "
              "Skipping task and plan workflows.")
    return pending


def _close_run(result: int, *, config_path: str,
               assent_dir: Path, selection: list[str] | None) -> int:
    """Complete a successful run with its matching integration workflow.

    ``selection`` is the exact named plan set the run covered; ``None`` keeps
    whole-project dynamic discovery. An unresolved selected plan defers
    integration. A nonzero task run starts no integration work.
    """
    if result != 0 or os.environ.get("ASSENT_PLAN_RUN") == "1":
        return result
    if selection == []:
        print("Integration workflow: no selected plan has anything left to integrate.")
        return 0
    if selection:
        incomplete: list[str] = []
        for plan_name in selection:
            try:
                cfg = load_config(config_path, plan_name)
            except AssentError as e:
                print(f"Config error: {e}")
                return 1
            if (not infer_plan_completion(cfg.tasks_dir).complete
                    or plan_workflow_requires_human(
                        cfg.tasks_dir, cfg.plan_workflow_step_count)):
                incomplete.append(plan_name)
        if incomplete:
            print("Integration workflow deferred: selected plan execution "
                  "requires human adjudication (" + ", ".join(incomplete) + ")")
            return 0
        return engine.run_selection_workflow(
            config_path, assent_dir, selection)
    if selection is None:
        return engine.run_dynamic_selection_workflow(config_path, assent_dir)
    raise AssertionError("run closeout selection is invalid")


def _dispatch(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "run":
        if len(args.plan_names) != len(set(args.plan_names)):
            parser.error("run does not allow duplicate PLAN names")
        if args.plan_names and args.jobs is not None:
            parser.error("run's --jobs cannot be used with PLAN")
    if args.command == "accept":
        if args.all_plans and args.plan_name:
            parser.error("accept's --all and PLAN cannot be used together")
        if not args.all_plans and not args.plan_name:
            parser.error("accept requires PLAN or --all")
        if len(args.plan_name) > 1 and len(args.plan_name) != len(set(args.plan_name)):
            parser.error("accept does not allow duplicate PLAN names")

    if args.command == "verify":
        if args.batch and args.plan_name:
            parser.error("verify's --batch and PLAN cannot be used together")
        if args.focus is not None:
            if args.batch:
                parser.error("verify's --focus and --batch cannot be used together")
            if len(args.plan_name) != 1:
                parser.error("verify's --focus requires exactly one PLAN")
        elif not args.batch:
            if not args.plan_name:
                parser.error("verify requires PLAN, a selected batch, or --batch")
            if len(args.plan_name) > 1 and len(args.plan_name) != len(set(args.plan_name)):
                parser.error("verify does not allow duplicate PLAN names")

    if args.command == "clean":
        if len(args.plan_name) != len(set(args.plan_name)):
            parser.error("clean does not allow duplicate PLAN names")

    if args.command == "archive":
        if len(args.plan_name) != len(set(args.plan_name)):
            parser.error("archive does not allow duplicate PLAN names")
        if args.restore:
            if args.all_plans:
                parser.error("archive's --restore and --all cannot be used together")
            if not args.plan_name:
                parser.error("archive --restore requires PLAN")
            if len(args.plan_name) > 1:
                parser.error("archive --restore takes exactly one PLAN")
        elif args.all_plans and args.plan_name:
            parser.error("archive's --all and PLAN cannot be used together")
        elif not args.all_plans and not args.plan_name:
            parser.error("archive requires PLAN or --all")

    if args.command == "init":
        return run_init(args.path, args.test, runtime_command=None)

    if args.command == "doctor":
        return run_doctor()

    # `ignored-dirs` acts on the Git worktree it runs in and writes only the
    # primary worktree's local manifest, so it deliberately skips the .assent
    # project config gate below: a source worktree carries no .assent at all.
    if args.command == "ignored-dirs":
        try:
            if args.operation == "status":
                return ignored_dirs_status()
            return ignored_dirs_declare(
                args.required, args.watch, args.none_required,
                args.not_required)
        except AssentError as e:
            print(f"ignored-dirs {args.operation}: failed ({e})")
            return 1

    try:
        assent_dir = validate_config(args.config)
    except AssentError as e:
        print(f"Config error: {e}")
        return 1

    # This is the common identity gate.  It runs before any selected command
    # operation, while dynamic discovery paths retain their own contracts.
    explicit: list[str] = []
    recognized: list[str] | set[str] = ()
    if args.command == "run":
        explicit = args.plan_names
    elif args.command in ("accept", "verify", "clean"):
        explicit = args.plan_name
    elif args.command in ("test", "status", "check", "report",
                          "reconcile", "reject", "rework"):
        explicit = [args.plan_name] if args.plan_name is not None else []
    elif args.command == "archive" and not args.restore:
        explicit = args.plan_name
        recognized = archive_recovery_names(assent_dir, explicit)
    if not _validate_explicit_plans(
            assent_dir, explicit, recognized=recognized):
        return 1

    if args.command == "run":
        selection: list[str] | None = None
        if args.plan_names:
            selection = _filter_accepted_run_plans(
                args.config, list(args.plan_names))
        closeout = functools.partial(
            _close_run, config_path=args.config,
            assent_dir=assent_dir, selection=selection)
        if selection is not None:
            return closeout(_dispatch_run_plans(args.config, selection))
        return closeout(run_all(args.config, assent_dir, args.jobs or 1))
    if args.command == "test":
        try:
            try:
                if args.plan_name is None:
                    cfg = load_main_runtime_config(args.config)
                else:
                    cfg = load_config(args.config, args.plan_name)
            except AssentError as e:
                print(f"Config error: {e}")
                return 1
            _print_runtime_test_start(cfg, plan_name=args.plan_name)
            return (_runtime_test_cli_exit_code(
                        engine.run_main_runtime_test(cfg))
                    if args.plan_name is None else
                    _run_plan_runtime_test(cfg))
        except KeyboardInterrupt:
            print("Test interrupted; runtime workflow state and candidate were preserved.")
            return 130
        except (AssentError, OSError) as error:
            print(f"Test refused: {error}")
            return 1
    if args.command == "accept":
        if args.all_plans:
            return accept_all(args.config, assent_dir)
        selected = args.plan_name
        if len(selected) >= 2:
            return accept_selected_batch(args.config, assent_dir, selected)
        try:
            cfg = load_config(args.config, selected[0])
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return accept_plan(cfg)
    if args.command == "archive":
        if args.all_plans:
            return archive_all(args.config, assent_dir)
        selected = args.plan_name
        if len(selected) > 1:
            return archive_selected(args.config, selected)
        try:
            cfg = load_config(args.config, selected[0])
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return restore_plan(cfg) if args.restore else archive_plan(cfg)
    if args.command == "verify":
        selected = args.plan_name
        if not args.batch:
            try:
                cfg = load_config(args.config, selected[0])
            except AssentError as e:
                print(f"Config error: {e}")
                return 1
        try:
            if args.batch:
                return verify_batch(args.config, assent_dir)
            if args.focus is not None:
                return engine.verify_focused(cfg, args.focus or None)
            if len(selected) >= 2:
                return verify_selected_batch(
                    args.config, assent_dir, selected)
            return verify_plan(cfg)
        except KeyboardInterrupt:
            print("\nverify interrupted; temporary resources were cleaned up.")
            return 130
    if args.command == "reconcile":
        try:
            cfg = load_config(args.config, args.plan_name)
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
            cfg = load_config(args.config, args.plan_name)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return reject_plan(cfg)
    if args.command == "rework":
        try:
            cfg = load_config(args.config, args.plan_name)
        except AssentError as e:
            print(f"Config error: {e}")
            return 1
        return rework_task(
            cfg, args.task, cascade=args.cascade,
            reason=args.reason, revert_code=args.revert_code)
    plan_names = list_task_plans(assent_dir)
    if args.command == "clean":
        selected = args.plan_name or plan_names
        if not selected:
            print("No plan with a task file found.")
            return 1
        configs = []
        for selected_plan in selected:
            try:
                configs.append(load_config(args.config, selected_plan))
            except AssentError as e:
                print(f"Config error: {e}")
                return 1
        return clean_plans(configs)
    if args.plan_name is None:
        if args.command == "check":
            return _dispatch_check_all(args.config, assent_dir, plan_names)
        else:
            return _dispatch_all(args.command, args.config, plan_names)
    else:
        plan_name = args.plan_name

    try:
        cfg = load_config(args.config, plan_name)
    except AssentError as e:
        print(f"Config error: {e}")
        return 1

    if args.command == "status":
        return inspection.status(cfg)
    if args.command == "check":
        return inspection.check(cfg)
    if args.command == "report":
        return inspection.report(cfg)
    return 2  # argparse required=True already guards this; defensive fallback


def _install_break_handler() -> None:
    """Windows-only: turn CTRL_BREAK_EVENT into KeyboardInterrupt.

    A whole-project ``run`` starts its child process with
    CREATE_NEW_PROCESS_GROUP, so an
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

    A whole-project ``run`` cannot rely on console signals to stop a child. Under tmux or
    mintty the child's pty is not a Win32 console, so
    ``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT)`` never reaches it and the
    parent waits forever. A stdin pipe is platform-independent and always
    reaches the child, so the parent closes it (or dies, which closes it too)
    and this daemon thread turns the resulting EOF -- or any byte -- into
    ``interrupt_main()``. That raises KeyboardInterrupt in the main thread, so
    the existing interrupt cleanup runs unchanged: WIP marking, the r-file
    interrupt entry, the wip checkpoint, exit 130. As a side effect a child
    whose parent crashes also cleans itself up instead of becoming an orphan.

    ``interrupt_main()`` only makes that exception pending until the main thread
    next runs bytecode, so it is paired with ``wake_stop_waiters()``. The wake is
    set first and the interrupt is marked immediately afterwards; this avoids
    Windows terminating a thread still inside an Event wait. An adapter queue
    wake also raises ``KeyboardInterrupt`` itself if thread scheduling happens
    between those two operations.

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
        wake_stop_waiters()
        _thread.interrupt_main()

    thread = threading.Thread(target=watch, name="assent-stdin-stop", daemon=True)
    thread.start()
    return thread


def _command_elapsed_line(command: str, elapsed: float, code: int, *,
                          interrupted: bool = False) -> str:
    """Word one end-to-end timing line for a finished or interrupted command.

    The label states the boundary explicitly, because `verify` also prints the
    verifier's own ``Full verification finished: elapsed ...`` line: that one is
    the expensive suite alone, this one additionally covers validation,
    candidate construction and cleanup.  A scheduler-spawned `run` child owns
    one plan rather than the human's invocation, so it is labeled apart from
    the parent's single end-to-end total.
    """
    verb = "interrupted" if interrupted else "finished"
    if command == "run" and os.environ.get(_PLAN_CHILD_ENV):
        subject = "Scheduled plan run"
    else:
        subject = f"Command `assent {command}`"
    return f"{subject} {verb}: elapsed {elapsed:.1f}s, exit code {code}"


def _dispatch_timed(actual_argv: list[str]) -> int:
    """Dispatch one invocation, reporting its end-to-end elapsed time.

    The timer covers everything the command does before it returns, and the
    reporting deliberately changes nothing else: the original diagnostics are
    already printed, the original exit code is returned unchanged, and an
    interrupt is re-raised after being timed.  A usage error or ``--help``
    leaves through ``SystemExit`` without a timing line, since neither is a run
    whose duration means anything.
    """
    command = actual_argv[0] if actual_argv else ""
    if command not in _TIMED_COMMANDS:
        return _dispatch(actual_argv)
    started = _monotonic()
    try:
        code = _dispatch(actual_argv)
    except KeyboardInterrupt:
        print(_command_elapsed_line(command, _monotonic() - started, 130,
                                    interrupted=True), flush=True)
        raise
    print(_command_elapsed_line(command, _monotonic() - started, code),
          flush=True)
    return code


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
        return _dispatch_timed(actual_argv)


def _exit_main() -> None:
    """Exit the executable without leaking a traceback for Ctrl+C.

    ``main(argv)`` remains an in-process entry point whose callers can handle
    ``KeyboardInterrupt`` themselves.  At the executable boundary, however, an
    interrupt that escapes cleanup is an ordinary interrupted command and has
    the conventional exit code 130.
    """
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    raise SystemExit(code)


if __name__ == "__main__":
    _exit_main()
