#!/usr/bin/env python3
"""Shared verification script: a task is not complete unless the verify command exits 0.

This script runs outside every AI session: a human starts it, or the scheduler runs
it through the integration workflow or on an explicit assent verify. A task
file's verify field must never name it -- a task's gate is
the narrow command proving its own acceptance, and the plan parser refuses a task
that points here.
Commands between the project-test markers are project-owned. Assent init
compares only the framework outside that block and leaves every example disabled
until the operator selects one.
"""

import concurrent.futures
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _utf8_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    return environment


def _decode_output(output: bytes, stream: str) -> tuple[str, bool]:
    try:
        return output.decode("utf-8"), False
    except UnicodeDecodeError:
        return output.decode("utf-8", errors="backslashreplace"), True


def _encoding_diagnostics(streams: list[str]) -> str:
    return "\n".join(
        f"Verifier output on {stream} was not valid UTF-8; "
        "undecodable bytes are escaped as \\xNN."
        for stream in streams
    )


def _append_encoding_diagnostics(stderr: str,
                                 streams: list[str]) -> str:
    diagnostics = _encoding_diagnostics(streams)
    if not diagnostics:
        return stderr
    if stderr and not stderr.endswith(("\n", "\r")):
        stderr += "\n"
    return f"{stderr}{diagnostics}\n"


def _configure_utf8_stdio() -> None:
    """Keep this verifier and every Python child on the same text encoding."""
    os.environ["PYTHONIOENCODING"] = "utf-8"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")


# On Windows, redirected stdio otherwise defaults to the system code page.
_configure_utf8_stdio()

# The verification target is always the cwd the scheduler sets; under isolated execution the script body still lives in the main tree.
ROOT = Path.cwd().resolve()

# Wall-clock origin for the whole verifier, reported on both the OK and FAIL exit.
_VERIFIER_STARTED = time.monotonic()

# (unittest phase seconds, module count, resolved worker count), or None when
# the unittest phase never ran.
_UNITTEST_TOTALS: tuple[float, int, int] | None = None

# Keep diff --check's conflict-marker detection without enforcing formatting.
DIFF_CHECK_CONFIG = (
    "core.whitespace=-blank-at-eol,-blank-at-eof,-space-before-tab,"
    "-indent-with-non-tab,-tab-in-indent"
)


def _print_verifier_totals() -> None:
    """Report the timing totals once, immediately before the OK/FAIL marker."""
    if _UNITTEST_TOTALS is not None:
        phase_elapsed, module_count, jobs = _UNITTEST_TOTALS
        print(f"unittest phase: {phase_elapsed:.2f}s "
              f"across {module_count} module(s) on {jobs} worker(s)")
    print(f"verifier total: {time.monotonic() - _VERIFIER_STARTED:.2f}s")


def fail(message: str) -> None:
    _print_verifier_totals()
    print(f"verify: FAIL - {message}")
    sys.exit(1)


def run(*cmd: str) -> None:
    # Resolve through PATH/PATHEXT first: on Windows a command installed as a
    # .bat/.cmd wrapper (flutter.bat, npm.cmd) is invisible to a bare
    # subprocess.run("flutter", ...) and would raise WinError 2. shell stays
    # False and the remaining argv elements are passed through untouched.
    program = shutil.which(cmd[0])
    if program is None:
        fail(f"command not found on PATH: {cmd[0]}")
    result = subprocess.run((program,) + cmd[1:], cwd=ROOT,
                            capture_output=True, env=_utf8_environment())
    stdout, bad_stdout = _decode_output(result.stdout, "stdout")
    stderr, bad_stderr = _decode_output(result.stderr, "stderr")
    bad_streams = [
        stream for stream, bad in (("stdout", bad_stdout), ("stderr", bad_stderr))
        if bad
    ]
    stderr = _append_encoding_diagnostics(stderr, bad_streams)
    if stdout:
        sys.stdout.write(stdout)
        sys.stdout.flush()
    if stderr:
        sys.stderr.write(stderr)
        sys.stderr.flush()
    if bad_streams:
        fail(f"command output was not valid UTF-8: {' '.join(cmd)}")
    if result.returncode != 0:
        fail(f"command failed (exit code {result.returncode}): {' '.join(cmd)}")


def check_committed_delta() -> None:
    """Check the candidate commit against its first parent, when one exists."""
    parent = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD^1"),
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="strict",
        env=_utf8_environment(),
    )
    if parent.returncode == 0:
        first_parent = parent.stdout.strip()
        if not first_parent:
            fail("git returned an empty first parent")
        run("git", "-c", DIFF_CHECK_CONFIG, "diff", "--check",
            first_parent, "HEAD")
    elif parent.returncode != 128:
        fail("unable to determine the candidate's first parent")


def _resolve_jobs(module_count: int) -> int:
    """Pick the thread pool size: env override, falling back to cpu-scaled default."""
    default = min(module_count, os.cpu_count() or 2)
    raw = os.environ.get("ASSENT_VERIFY_JOBS")
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return default
    return value


def _print_slowest_modules(results: dict[str, tuple]) -> None:
    """Rank the slowest ten modules, or every module when fewer than ten ran.

    Ranking uses the same two-decimal duration the line below prints, so two
    modules a reader sees as equally slow are always ordered by module name
    rather than by a difference no printed digit shows.
    """
    ranked = sorted(results.items(),
                    key=lambda item: (-round(item[1][1], 2), item[0]))
    if len(ranked) >= 10:
        ranked = ranked[:10]
    print("Slowest test modules:")
    for rank, (name, result) in enumerate(ranked, start=1):
        returncode, elapsed = result[0], result[1]
        status = "PASS" if returncode == 0 else "FAIL"
        print(f"  {rank}. {name} {elapsed:.2f}s {status}")


def run_unittest_parallel(start_dir: str = "tests", jobs: int | None = None) -> None:
    """Run each start_dir/test_*.py module in its own subprocess, concurrently.

    Process isolation (not threads) is deliberate: unittest modules mutate
    process-global state (os.chdir, os.environ), so sharing one interpreter
    across modules would let them corrupt each other. Threads here only wait
    on subprocesses; they never run test code directly.
    """
    test_dir = ROOT / start_dir
    modules = sorted(path.stem for path in test_dir.glob("test_*.py"))
    if not modules:
        fail(f"no test_*.py modules found under {start_dir}")

    if jobs is None:
        jobs = _resolve_jobs(len(modules))

    def run_module(name: str):
        started = time.monotonic()
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", f"{start_dir}.{name}"],
            cwd=ROOT, capture_output=True, env=_utf8_environment(),
        )
        elapsed = time.monotonic() - started
        stdout, bad_stdout = _decode_output(proc.stdout, "stdout")
        stderr, bad_stderr = _decode_output(proc.stderr, "stderr")
        bad_streams = [
            stream for stream, bad in (("stdout", bad_stdout), ("stderr", bad_stderr))
            if bad
        ]
        stderr = _append_encoding_diagnostics(stderr, bad_streams)
        returncode = proc.returncode
        if bad_streams and returncode == 0:
            returncode = 1
        return returncode, elapsed, stdout, stderr

    # Live lines follow completion order, so a long verification never looks
    # silent; the deterministic ranking below is what tests and readers compare.
    results = {}
    phase_started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(run_module, name): name for name in modules}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            results[name] = future.result()
            returncode, elapsed = results[name][0], results[name][1]
            status = "pass" if returncode == 0 else "fail"
            print(f"{name}: {status} ({elapsed:.2f}s)", flush=True)

    global _UNITTEST_TOTALS
    _UNITTEST_TOTALS = (time.monotonic() - phase_started, len(modules), jobs)

    _print_slowest_modules(results)

    failed = [name for name in modules if results[name][0] != 0]
    if failed:
        for name in failed:
            _returncode, _elapsed, stdout, stderr = results[name]
            print(f"--- {name} output ---")
            if stdout:
                print(stdout)
            if stderr:
                print(stderr)
        fail(f"test module(s) failed: {', '.join(failed)}")


# --- Worktree integrity check (keep) ---
# Whitespace is formatting, not an integration failure. Conflict markers fail.
run("git", "-c", DIFF_CHECK_CONFIG, "diff", "--check")
check_committed_delta()

# --- Project test commands begin (project-owned) ---
# Assent init activates exactly one line; the
# numbering matches the assent init menu.

# 0. Custom command (anything not covered below):
#    assent init --test custom:"<your test command>"

# 1. Python (unittest):
# run_unittest_parallel()

# 2. Python (pytest):
# run("ruff", "check", ".")
# run("ruff", "format", "--check", ".")
# run("pytest")

# 3. Node / TypeScript:
# run("npx", "prettier", "--check", ".")
# run("npx", "eslint", ".")
# run("npm", "test")

# 4. Flutter / Dart:
# run("dart", "format", "--output=none", "--set-exit-if-changed", ".")
# run("flutter", "analyze")
# run("flutter", "test")

# 5. C# / .NET:
# run("dotnet", "test")

# 6. Java (Maven):
# run("mvn", "test")

# 7. Java (Gradle):
# run("gradle", "test")

# 8. C / C++ (CMake + CTest; "build" is only the common default -- point
#    --test-dir at whatever binary directory your project actually
#    configures, e.g. cmake-build-debug or out/build/<preset>):
# run("ctest", "--test-dir", "build", "--output-on-failure")

# 9. C / C++ (Make):
# run("make", "test")

# --- Project test commands end ---

_print_verifier_totals()
print("verify: OK")
