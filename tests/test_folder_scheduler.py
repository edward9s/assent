"""Tests for the all-folders scheduler's dependency order, concurrency, and
stuck-chain detection.

Chinese literals that remain are deliberate user-authored data (task titles,
goals, acceptance text, child output lines) used to prove that non-English
data passes through the scheduler verbatim rather than being translated as
output."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agents.folder_scheduler import _send_interrupt, _start_folder, run_all
from agents.plan import set_status
from agents.terminal_log import terminal_logging

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def task_text(status: str) -> str:
    return (
        'title = "任務"\n'
        'deps = []\n'
        'model = "lite"\n'
        f'status = "{status}"\n'
        'scope = ["agents/"]\n'
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
        self.agents_dir = self.root / ".agents"
        self.agents_dir.mkdir()
        self.git_marker = self.root / ".git"
        self.git_marker.mkdir()
        self.config = self.agents_dir / "agents.toml"
        self.config.write_text("", encoding="utf-8")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def make_folder(self, name: str, status: str = "TODO",
                    after: tuple[str, ...] = ()) -> Path:
        folder = self.agents_dir / name
        folder.mkdir()
        task = folder / "t001_task.e.toml"
        task.write_text(task_text(status), encoding="utf-8")
        if after:
            values = ", ".join(f'"{item}"' for item in after)
            (folder / "_folder.toml").write_text(
                f"after = [{values}]\n", encoding="utf-8")
        return task


class TestRunAll(FolderSchedulerTestCase):
    def test_child_uses_utf8_merged_text_pipe_and_process_group(self):
        process = object()
        with patch("agents.folder_scheduler.subprocess.Popen",
                   return_value=process) as popen:
            actual = _start_folder(str(self.config), "work")

        self.assertIs(actual, process)
        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[:3], [sys.executable, "-m", "agents"])
        self.assertEqual(command[3:5], ["run", "work"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
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
                "agents.folder_scheduler._start_folder",
                return_value=OutputProcess(task, "第一列\n最後一列")):
            code = run_all(str(self.config), self.agents_dir)

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
                "agents.folder_scheduler._start_folder", side_effect=fake_start):
            code = run_all(str(self.config), self.agents_dir, jobs=2)

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
                "agents.folder_scheduler._start_folder",
                return_value=OutputProcess(task, raw)):
            code = run_all(str(self.config), self.agents_dir)

        self.assertEqual(code, 0)
        self.assertIn("[encoded] 標準錯誤\n", out.getvalue())
        self.assertIn("[encoded] bad:\ufffd\n", out.getvalue())

    def test_terminal_only_forwarding_does_not_enter_root_log_or_child_log(self):
        task = self.make_folder("work")
        child_log = task.parent / "_agents.log"
        terminal = io.StringIO()

        def fake_start(_config, _folder):
            child_log.write_text("子行程原始訊息\n", encoding="utf-8")
            return OutputProcess(task, "子行程原始訊息\n")

        argv = ["run", "--all", "--config", str(self.config)]
        with contextlib.redirect_stdout(terminal):
            with terminal_logging(argv) as root_log, patch(
                    "agents.folder_scheduler._start_folder",
                    side_effect=fake_start):
                code = run_all(str(self.config), self.agents_dir)

        self.assertEqual(code, 0)
        terminal_text = terminal.getvalue()
        root_text = root_log.read_text(encoding="utf-8")
        self.assertIn("[work] 子行程原始訊息", terminal_text)
        self.assertIn("AGENTS START", root_text)
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
                "agents.folder_scheduler.parse_folder_dependency_graph") as parse, \
                patch("agents.folder_scheduler._start_folder") as start:
            code = run_all(str(self.config), self.agents_dir)

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

        with patch("agents.folder_scheduler._start_folder", side_effect=fake_start):
            code = run_all(str(self.config), self.agents_dir)

        self.assertEqual(code, 0)
        self.assertEqual(started, ["first", "second", "third"])

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

        with patch("agents.folder_scheduler._start_folder", side_effect=fake_start):
            code = run_all(str(self.config), self.agents_dir, jobs=2)

        self.assertEqual(code, 0)
        self.assertEqual(started, ["alpha", "beta"])
        self.assertEqual(first_poll_started_count[0], 2)

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
        with patch("agents.folder_scheduler._start_folder",
                   side_effect=lambda _config, folder: processes[folder]):
            code = run_all(str(self.config), self.agents_dir, jobs=2)

        self.assertEqual(code, 0)

    def test_blocked_prerequisite_reports_complete_chain(self):
        self.make_folder("base", "BLOCKED")
        self.make_folder("middle", after=("base",))
        self.make_folder("leaf", after=("middle",))
        out = io.StringIO()

        with contextlib.redirect_stdout(out), patch(
                "agents.folder_scheduler._start_folder") as start:
            code = run_all(str(self.config), self.agents_dir)

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
                "agents.folder_scheduler._start_folder",
                return_value=FailedProcess()):
            code = run_all(str(self.config), self.agents_dir)

        self.assertEqual(code, 1)
        self.assertIn("Work folder failed: work (exit code 7", out.getvalue())
        self.assertIn("_agents.log", out.getvalue())

    def test_status_control_c_exit_code_is_treated_as_interrupt(self):
        self.make_folder("work")

        class ControlCExitProcess:
            def poll(self):
                return 3221225786

        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch(
                "agents.folder_scheduler._start_folder",
                return_value=ControlCExitProcess()):
            code = run_all(str(self.config), self.agents_dir)

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

            def wait(self):
                self.waited = True
                return 130

        process = InterruptedProcess()
        with patch("agents.folder_scheduler._start_folder",
                   return_value=process), patch(
                       "agents.folder_scheduler._send_interrupt") as send:
            code = run_all(str(self.config), self.agents_dir)

        self.assertEqual(code, 130)
        send.assert_called_once_with(process)
        self.assertTrue(process.waited)


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
        from agents.__main__ import _install_break_handler
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
