"""Tests for the all-folders scheduler's dependency order, concurrency, and
stuck-chain detection.

Chinese literals that remain are deliberate user-authored data (task titles,
goals, acceptance text, child output lines) used to prove that non-English
data passes through the scheduler verbatim rather than being translated as
output."""
import contextlib
import io
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from assent.folder_scheduler import (_interrupt_and_wait, _kill_tree,
                                     _send_interrupt, _start_folder, run_all)
from assent.plan import set_status
from assent.terminal_log import terminal_logging

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def task_text(status: str) -> str:
    return (
        'title = "任務"\n'
        'deps = []\n'
        'model = "lite"\n'
        f'status = "{status}"\n'
        'scope = ["assent/"]\n'
        'verify = "python -m unittest"\n'
        'goal = "完成任務"\n'
        'acceptance = "驗證通過"\n'
    )


class FinishedProcess:
    """Stand-in for a real AI child process: completes the given folder the
    first time it is observed."""

    def __init__(self, task: Path, on_finish=None) -> None:
        self.task = task
        self.on_finish = on_finish
        self.finished = False

    def poll(self):
        if not self.finished:
            self.finished = True
            set_status(self.task, "DONE")
            if self.on_finish is not None:
                self.on_finish()
        return 0


class OutputProcess(FinishedProcess):
    """A completed child process stand-in with a text pipe."""

    def __init__(self, task: Path, output: str | bytes) -> None:
        super().__init__(task)
        if isinstance(output, bytes):
            self.stdout = io.TextIOWrapper(
                io.BytesIO(output), encoding="utf-8", errors="replace")
        else:
            self.stdout = io.StringIO(output)


class FolderSchedulerTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.assent_dir = self.root / ".assent"
        self.assent_dir.mkdir()
        self.git_marker = self.root / ".git"
        self.git_marker.mkdir()
        self.config = self.assent_dir / "assent.toml"
        self.config.write_text("", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_folder(self, name: str, status: str = "TODO",
                    after: tuple[str, ...] = ()) -> Path:
        folder = self.assent_dir / name
        folder.mkdir()
        task = folder / "t001_task.e.toml"
        task.write_text(task_text(status), encoding="utf-8")
        if after:
            values = ", ".join(f'"{item}"' for item in after)
            (folder / "_folder.toml").write_text(
                f"after = [{values}]\n", encoding="utf-8")
        return task

    def archive_roster(self, *folders: str) -> None:
        """Register folders in the archive roster as if they had been archived
        (their live directories intentionally do not exist)."""
        entries = "".join(
            f'[[archived]]\nfolder = "{name}"\n'
            f'archived_at = "2026-01-01T00:00:00Z"\n\n'
            for name in folders)
        (self.assent_dir / "_archived.toml").write_text(
            entries, encoding="utf-8")


class TestRunAll(FolderSchedulerTestCase):
    def test_child_uses_utf8_merged_text_pipe_and_process_group(self):
        process = object()
        with patch("assent.folder_scheduler.subprocess.Popen",
                   return_value=process) as popen:
            actual = _start_folder(str(self.config), "work")

        self.assertIs(actual, process)
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[:3], [sys.executable, "-m", "assent"])
        self.assertEqual(command[3:5], ["run", "work"])
        # stdin is the stop channel, so it must be a pipe and the child must be
        # told to watch it.
        self.assertEqual(options["stdin"], subprocess.PIPE)
        self.assertEqual(options["env"]["ASSENT_STDIN_STOP"], "1")
        self.assertEqual(options["stdout"], subprocess.PIPE)
        self.assertEqual(options["stderr"], subprocess.STDOUT)
        self.assertIs(options["text"], True)
        self.assertEqual(options["encoding"], "utf-8")
        self.assertEqual(options["errors"], "replace")
        if os.name == "nt":
            self.assertEqual(options["creationflags"],
                             subprocess.CREATE_NEW_PROCESS_GROUP)
            self.assertNotIn("start_new_session", options)
        else:
            self.assertIs(options["start_new_session"], True)
            self.assertNotIn("creationflags", options)

    def test_jobs_one_forwards_each_line_with_folder_prefix(self):
        task = self.make_folder("serial")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder",
                return_value=OutputProcess(task, "第一列\n最後一列")):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 0)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines.count("[serial] 第一列"), 1)
        self.assertEqual(lines.count("[serial] 最後一列"), 1)
        self.assertLess(lines.index("[serial] 最後一列"),
                        lines.index("Work folder complete: serial (exit code 0)"))

    def test_jobs_two_serializes_lines_with_correct_source(self):
        tasks = {
            "alpha": self.make_folder("alpha"),
            "beta": self.make_folder("beta"),
        }
        outputs = {
            "alpha": "甲一\n甲二\n",
            "beta": "乙一\n乙二\n",
        }
        out = io.StringIO()

        def fake_start(_config, folder):
            return OutputProcess(tasks[folder], outputs[folder])

        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder", side_effect=fake_start):
            code = run_all(str(self.config), self.assent_dir, jobs=2)

        self.assertEqual(code, 0)
        forwarded = [line for line in out.getvalue().splitlines()
                     if line.startswith("[")]
        self.assertCountEqual(
            forwarded,
            ["[alpha] 甲一", "[alpha] 甲二", "[beta] 乙一", "[beta] 乙二"],
        )

    def test_merged_stderr_bad_utf8_and_unterminated_line_are_forwarded(self):
        task = self.make_folder("encoded")
        out = io.StringIO()
        raw = "標準錯誤\n".encode("utf-8") + b"bad:\xff"
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder",
                return_value=OutputProcess(task, raw)):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 0)
        self.assertIn("[encoded] 標準錯誤\n", out.getvalue())
        self.assertIn("[encoded] bad:\ufffd\n", out.getvalue())

    def test_terminal_only_forwarding_does_not_enter_root_log_or_child_log(self):
        task = self.make_folder("work")
        child_log = task.parent / "_assent.log"
        terminal = io.StringIO()

        def fake_start(_config, _folder):
            child_log.write_text("子行程原始訊息\n", encoding="utf-8")
            return OutputProcess(task, "子行程原始訊息\n")

        argv = ["run", "--all", "--config", str(self.config)]
        with contextlib.redirect_stdout(terminal):
            with terminal_logging(argv) as root_log, patch(
                    "assent.folder_scheduler._start_folder",
                    side_effect=fake_start):
                code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 0)
        terminal_text = terminal.getvalue()
        root_text = root_log.read_text(encoding="utf-8")
        self.assertIn("[work] 子行程原始訊息", terminal_text)
        self.assertIn("ASSENT START", root_text)
        self.assertIn("Starting work folder: work", root_text)
        self.assertIn("Work folder complete: work", root_text)
        self.assertNotIn("子行程原始訊息", root_text)
        self.assertEqual(child_log.read_text(encoding="utf-8"),
                         "子行程原始訊息\n")

    def test_no_git_rejects_completed_folders_before_inspection(self):
        self.make_folder("done", "DONE")
        self.make_folder("skipped", "SKIP")
        self.git_marker.rmdir()
        out = io.StringIO()

        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler.parse_folder_dependency_graph") as parse, \
                patch("assent.folder_scheduler._start_folder") as start:
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 1)
        self.assertIn("This project has no git repository yet; run git init first",
                      out.getvalue())
        parse.assert_not_called()
        start.assert_not_called()

    def test_completion_unlocks_downstream_in_topological_order(self):
        first = self.make_folder("first")
        second = self.make_folder("second", after=("first",))
        third = self.make_folder("third", after=("second",))
        tasks = {"first": first, "second": second, "third": third}
        started = []

        def fake_start(_config, folder):
            started.append(folder)
            return FinishedProcess(tasks[folder])

        with patch("assent.folder_scheduler._start_folder", side_effect=fake_start):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 0)
        self.assertEqual(started, ["first", "second", "third"])

    def test_after_referencing_archived_upstream_schedules_downstream(self):
        # Reproduces the incident: the live upstream was archived, so its name
        # survives only in the roster while a live downstream still depends on
        # it.  run --all's inline runnable check used to do plans[name] and
        # raise KeyError; the roster-aware predicate must instead treat the
        # archived upstream as complete and schedule the downstream.
        downstream = self.make_folder("downstream", after=("upstream_archived",))
        self.archive_roster("upstream_archived")
        started = []

        def fake_start(_config, folder):
            started.append(folder)
            return FinishedProcess(downstream)

        with patch("assent.folder_scheduler._start_folder",
                   side_effect=fake_start):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 0)
        self.assertEqual(started, ["downstream"])

    def test_after_referencing_unknown_name_fails_closed_with_clear_error(self):
        # A name in neither the live folders nor the roster is refused with a
        # clear scheduling error naming it, not a traceback, and starts nothing.
        self.make_folder("downstream", after=("ghost",))
        out = io.StringIO()

        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder") as start:
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 1)
        self.assertIn("Folder scheduling failed", out.getvalue())
        self.assertIn("ghost", out.getvalue())
        start.assert_not_called()

    def test_real_git_launch_prints_resolved_stack_decision_before_child(self):
        task = self.make_folder("downstream")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._has_usable_git", return_value=True), patch(
                "assent.folder_scheduler._stack_launch_decision",
                return_value=("Stack decision: downstream: base abc from unaccepted "
                              "upstream upstream @ abc; worktree create.", None)), patch(
                "assent.folder_scheduler._start_folder",
                return_value=FinishedProcess(task)):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("unaccepted upstream upstream", text)
        self.assertLess(text.index("Stack decision:"),
                        text.index("Starting work folder: downstream"))

    def test_stack_refusal_does_not_start_a_child(self):
        self.make_folder("downstream")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._has_usable_git", return_value=True), patch(
                "assent.folder_scheduler._stack_launch_decision",
                return_value=(None, "multiple unaccepted upstream folders")), patch(
                "assent.folder_scheduler._start_folder") as start:
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 1)
        self.assertIn("Work folder refused: downstream", out.getvalue())
        start.assert_not_called()

    def test_jobs_two_starts_two_independent_folders_before_polling(self):
        alpha = self.make_folder("alpha")
        beta = self.make_folder("beta")
        tasks = {"alpha": alpha, "beta": beta}
        started = []
        first_poll_started_count = []

        def fake_start(_config, folder):
            started.append(folder)
            return FinishedProcess(
                tasks[folder],
                lambda: first_poll_started_count.append(len(started)))

        with patch("assent.folder_scheduler._start_folder", side_effect=fake_start):
            code = run_all(str(self.config), self.assent_dir, jobs=2)

        self.assertEqual(code, 0)
        self.assertEqual(started, ["alpha", "beta"])
        self.assertEqual(first_poll_started_count[0], 2)

    def test_jobs_two_never_starts_dependent_while_upstream_is_active(self):
        upstream = self.make_folder("upstream")
        downstream = self.make_folder("downstream", after=("upstream",))
        independent = self.make_folder("independent")
        tasks = {
            "upstream": upstream,
            "downstream": downstream,
            "independent": independent,
        }
        started = []

        def fake_start(_config, folder):
            if folder == "downstream":
                self.assertEqual(
                    next(task for name, task in tasks.items()
                         if name == "upstream").read_text(encoding="utf-8"),
                    task_text("DONE"))
            started.append(folder)
            return FinishedProcess(tasks[folder])

        with patch("assent.folder_scheduler._start_folder",
                   side_effect=fake_start):
            code = run_all(str(self.config), self.assent_dir, jobs=2)

        self.assertEqual(code, 0)
        self.assertEqual(started[:2], ["independent", "upstream"])
        self.assertEqual(started[2], "downstream")

    def test_recalculation_does_not_read_a_running_folders_partial_write(self):
        alpha = self.make_folder("alpha")
        beta = self.make_folder("beta")

        class WritingProcess:
            def __init__(self):
                self.poll_count = 0

            def poll(self):
                self.poll_count += 1
                if self.poll_count == 1:
                    beta.write_text("status = [\n", encoding="utf-8")
                    return None
                beta.write_text(task_text("DONE"), encoding="utf-8")
                return 0

        processes = {
            "alpha": FinishedProcess(alpha),
            "beta": WritingProcess(),
        }
        with patch("assent.folder_scheduler._start_folder",
                   side_effect=lambda _config, folder: processes[folder]):
            code = run_all(str(self.config), self.assent_dir, jobs=2)

        self.assertEqual(code, 0)

    def test_blocked_prerequisite_reports_complete_chain(self):
        self.make_folder("base", "BLOCKED")
        self.make_folder("middle", after=("base",))
        self.make_folder("leaf", after=("middle",))
        out = io.StringIO()

        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder") as start:
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 1)
        self.assertIn("BLOCKED", out.getvalue())
        self.assertIn("leaf -> middle -> base -> t001(BLOCKED)", out.getvalue())
        start.assert_not_called()

    def test_child_failure_stops_with_log_location(self):
        self.make_folder("work")

        class FailedProcess:
            def poll(self):
                return 7

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder",
                return_value=FailedProcess()):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 1)
        self.assertIn("Work folder failed: work (exit code 7", out.getvalue())
        self.assertIn("_assent.log", out.getvalue())

    def test_status_control_c_exit_code_is_treated_as_interrupt(self):
        self.make_folder("work")

        class ControlCExitProcess:
            def poll(self):
                return 3221225786

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder",
                return_value=ControlCExitProcess()):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 130)
        self.assertIn("Work folder interrupted: work (exit code 3221225786)",
                      out.getvalue())
        self.assertNotIn("Work folder failed", out.getvalue())

    def test_keyboard_interrupt_is_forwarded_and_waits_for_child(self):
        self.make_folder("work")

        class InterruptedProcess:
            def __init__(self):
                self.waited = False

            def poll(self):
                raise KeyboardInterrupt

            def wait(self, timeout=None):
                self.waited = True
                return 130

        process = InterruptedProcess()
        with patch("assent.folder_scheduler._start_folder",
                   return_value=process), patch(
                       "assent.folder_scheduler._send_interrupt") as send:
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 130)
        send.assert_called_once_with(process)
        self.assertTrue(process.waited)


class InterruptingStream(io.StringIO):
    """Stand-in for the terminal-log sink, raising KeyboardInterrupt on chosen
    writes so a second Ctrl+C can be aimed at an exact point of the cleanup."""

    def __init__(self, interrupt_at: set[int]) -> None:
        super().__init__()
        self.interrupt_at = interrupt_at
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes in self.interrupt_at:
            raise KeyboardInterrupt
        return super().write(text)


class StubPipe:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class DeafChild:
    """Child stand-in that ignores every signal sent to it.

    This is the tmux situation in miniature: the console/process-group signal
    is accepted and dropped, so only the stdin stop channel -- or a forced
    kill -- can end the process. ``reads_stdin`` picks which.
    """

    def __init__(self, *, reads_stdin: bool, pid: int = 4242) -> None:
        self.stdin = StubPipe()
        self.pid = pid
        self.reads_stdin = reads_stdin
        self.returncode = None
        self.signals: list[int] = []
        self.killed = False

    def poll(self):
        return self.returncode

    def send_signal(self, number) -> None:
        self.signals.append(number)

    def die(self, *_args, **_kwargs) -> None:
        """What a real forced tree termination does to this process."""
        self.killed = True

    def wait(self, timeout=None):
        if self.reads_stdin and self.stdin.closed:
            self.returncode = 130
        elif self.killed:
            self.returncode = -9
        else:
            raise subprocess.TimeoutExpired("assent", timeout)
        return self.returncode


class TestStopChannelAndEscalation(FolderSchedulerTestCase):
    """The stdin stop channel is the primary route out; a forced tree
    termination is the backstop when neither the channel nor a signal is
    honoured, so the parent never waits without a bound."""

    def interrupt(self, active: dict, out: io.StringIO | None = None) -> str:
        """Run _interrupt_and_wait with both forced-kill syscalls stubbed to
        the death they would really cause."""
        out = io.StringIO() if out is None else out
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler.subprocess.run",
                side_effect=lambda *a, **k: [
                    child.die() for child in active.values()]), patch(
                "assent.folder_scheduler.os.killpg", create=True,
                side_effect=lambda *a, **k: [
                    child.die() for child in active.values()]):
            _interrupt_and_wait(active, {}, queue.Queue())
        return out.getvalue()

    def test_closing_stdin_lets_a_signal_deaf_child_finish_itself(self):
        child = DeafChild(reads_stdin=True)

        out = self.interrupt({"work": child})

        self.assertTrue(child.stdin.closed)
        self.assertIn("Work folder finished: work (exit code 130)", out)
        self.assertNotIn("Escalating work folder", out)

    def test_child_deaf_to_signal_and_stdin_is_force_terminated(self):
        child = DeafChild(reads_stdin=False)

        out = self.interrupt({"work": child})

        self.assertTrue(child.stdin.closed)
        self.assertIn("Escalating work folder: work", out)
        self.assertIn("no exit within 60 seconds of the stop request", out)
        self.assertEqual(child.returncode, -9)

    def test_undeliverable_stop_request_skips_the_grace_period(self):
        child = DeafChild(reads_stdin=False)
        child.stdin.close = lambda: (_ for _ in ()).throw(ValueError("closed"))

        out = self.interrupt({"work": child})

        self.assertIn("Failed to forward interrupt signal: work", out)
        self.assertIn("the stop request could not be delivered", out)
        self.assertNotIn("no exit within 60 seconds", out)
        self.assertEqual(child.returncode, -9)

    def test_interrupt_while_the_cleanup_message_is_written_still_cleans_up(self):
        """The announcement goes through the terminal-log sink, so a second
        Ctrl+C can land there before any child has been notified."""
        child = DeafChild(reads_stdin=False)

        out = self.interrupt({"work": child}, InterruptingStream({1}))

        self.assertIn("Second interrupt (Ctrl+C)", out)
        self.assertTrue(child.killed)
        self.assertEqual(child.returncode, -9)
        self.assertTrue(child.stdin.closed)

    def test_repeated_interrupts_during_forced_cleanup_kill_and_reap(self):
        first = DeafChild(reads_stdin=False)
        second = DeafChild(reads_stdin=False, pid=4243)

        out = self.interrupt({"a": first, "b": second},
                             InterruptingStream(set(range(1, 6))))

        self.assertIn("Second interrupt (Ctrl+C)", out)
        for folder, child in (("a", first), ("b", second)):
            self.assertTrue(child.killed, folder)
            self.assertEqual(child.returncode, -9, folder)
            self.assertTrue(child.stdin.closed, folder)

    def test_second_interrupt_force_kills_everything_and_returns_130(self):
        self.make_folder("work")
        child = DeafChild(reads_stdin=False)

        ctrl_c = iter([True])  # only the very first poll is the user's Ctrl+C

        def first_ctrl_c():
            if next(ctrl_c, False):
                raise KeyboardInterrupt
            return -9 if child.killed else None

        def second_ctrl_c(timeout=None):
            if child.killed:
                child.returncode = -9
                return -9
            raise KeyboardInterrupt

        def fake_run(args, **kwargs):
            # run_all also shells out to git; only taskkill means "die".
            if args and args[0] == "taskkill":
                child.die()
                return subprocess.CompletedProcess(args, 0)
            return subprocess.CompletedProcess(args, 1)

        child.poll = first_ctrl_c
        child.wait = second_ctrl_c
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler._start_folder",
                return_value=child), patch(
                "assent.folder_scheduler.subprocess.run",
                side_effect=fake_run), patch(
                "assent.folder_scheduler.os.killpg", create=True,
                side_effect=lambda *a, **k: child.die()):
            code = run_all(str(self.config), self.assent_dir)

        self.assertEqual(code, 130)
        self.assertIn("Second interrupt (Ctrl+C)", out.getvalue())
        self.assertTrue(child.killed)

    @unittest.skipUnless(os.name == "nt", "taskkill is the Windows tree kill")
    def test_windows_tree_kill_uses_taskkill_with_tree_and_force(self):
        child = DeafChild(reads_stdin=False, pid=1234)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler.subprocess.run",
                side_effect=lambda *a, **k: child.die()) as run:
            _kill_tree("work", child, "test")

        self.assertEqual(run.call_args.args[0],
                         ["taskkill", "/PID", "1234", "/T", "/F"])
        self.assertIn("taskkill /T /F on pid 1234", out.getvalue())

    @unittest.skipIf(os.name == "nt", "process groups are the POSIX tree kill")
    def test_posix_tree_kill_escalates_sigterm_to_sigkill(self):
        child = DeafChild(reads_stdin=False, pid=1234)  # never dies
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "assent.folder_scheduler.os.killpg") as killpg:
            _kill_tree("work", child, "test")

        self.assertEqual([call.args for call in killpg.call_args_list],
                         [(1234, signal.SIGTERM), (1234, signal.SIGKILL)])
        self.assertIn("SIGKILL to process group 1234", out.getvalue())


class TestStdinStopChannelChild(unittest.TestCase):
    """Real check with a real child: closing stdin ends a process that never
    reacts to a signal.

    Closing the pipe is exactly what the OS does for us when the parent dies,
    so the same path is what stops a child from being orphaned by a parent
    crash.
    """

    _PROBE = textwrap.dedent(
        """
        import sys, time
        from assent.__main__ import _start_stdin_stop_watcher
        _start_stdin_stop_watcher()
        try:
            print("READY", flush=True)
            deadline = time.time() + 30
            while time.time() < deadline:
                # Short sleeps give the pending KeyboardInterrupt a bytecode
                # boundary to land on; this mirrors engine's segmented
                # non-tty countdown.
                time.sleep(0.005)
        except KeyboardInterrupt:
            sys.exit(130)
        sys.exit(1)
        """
    )

    def test_stdin_close_stops_a_child_that_ignores_signals(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_PROJECT_ROOT)
        env["ASSENT_STDIN_STOP"] = "1"
        process = subprocess.Popen(
            [sys.executable, "-c", self._PROBE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            process.stdin.close()
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - only hit on a hang
            process.kill()
            raise
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            for stream in (process.stdin, process.stdout):
                if stream is not None and not stream.closed:
                    stream.close()
        self.assertEqual(returncode, 130)


@unittest.skipUnless(os.name == "nt", "CTRL_BREAK_EVENT cleanup is Windows-only")
class TestBreakHandlerInterrupt(unittest.TestCase):
    """Real check: a child with the handler installed turns CTRL_BREAK_EVENT
    into a KeyboardInterrupt."""

    # Wait for the signal with a "short-sleep loop" instead of a single
    # time.sleep or a busy loop:
    #   * A single time.sleep is not woken by SIGBREAK on Windows;
    #   * A pure busy loop lets the main thread hog the GIL, starving the
    #     console control handler thread Windows spawns for the signal, so
    #     SIGBREAK sometimes arrives too late and the OS's default handler
    #     terminates the process directly (exit code 3221225786).
    # Each short sleep releases the GIL so the handler thread can run; the
    # bytecode check point on waking then turns a pending SIGBREAK into
    # KeyboardInterrupt. This also mirrors a real run blocked on a child
    # process wait(), which releases the GIL. Everything after the handler is
    # registered runs inside try, so the signal is caught wherever it lands.
    _PROBE = textwrap.dedent(
        """
        import sys, time
        from assent.__main__ import _install_break_handler
        _install_break_handler()
        try:
            print("READY", flush=True)
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(0.005)
        except KeyboardInterrupt:
            sys.exit(130)
        sys.exit(1)
        """
    )

    def test_ctrl_break_event_reaches_keyboard_interrupt_and_exits_130(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_PROJECT_ROOT)
        process = subprocess.Popen(
            [sys.executable, "-c", self._PROBE],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            text=True,
        )
        try:
            self.assertEqual(process.stdout.readline().strip(), "READY")
            _send_interrupt(process)
            returncode = process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - only hit on a hang
            process.kill()
            raise
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdout is not None:
                process.stdout.close()
        self.assertEqual(returncode, 130)


if __name__ == "__main__":
    unittest.main()
