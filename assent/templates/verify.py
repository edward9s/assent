#!/usr/bin/env python3
"""Shared verification script: a task is not complete unless the verify command exits 0.

A task file's verify field points to this script by default (python .assent/verify.py);
individual tasks may swap in a faster or stricter command.
TODO: replace the "project checks" examples below with your project's actual check commands.
"""

import concurrent.futures
import os
import subprocess
import sys
import time
from pathlib import Path

# On Windows, stdout redirected to a pipe/file defaults to the system code page, which garbles non-ASCII text
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# The verification target is always the cwd the scheduler sets; under isolated execution the script body still lives in the main tree.
ROOT = Path.cwd().resolve()


def fail(message: str) -> None:
    print(f"verify: FAIL - {message}")
    sys.exit(1)


def run(*cmd: str) -> None:
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        fail(f"command failed (exit code {result.returncode}): {' '.join(cmd)}")


def check_committed_delta() -> None:
    """Check the candidate commit against its first parent, when one exists."""
    parent = subprocess.run(
        ("git", "rev-parse", "--verify", "HEAD^1"),
        cwd=ROOT,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if parent.returncode == 0:
        first_parent = parent.stdout.strip()
        if not first_parent:
            fail("git returned an empty first parent")
        run("git", "diff", "--check", first_parent, "HEAD")
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
            cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.monotonic() - started
        return proc.returncode, elapsed, proc.stdout, proc.stderr

    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as executor:
        futures = {executor.submit(run_module, name): name for name in modules}
        for future in concurrent.futures.as_completed(futures):
            results[futures[future]] = future.result()

    failed = [name for name in modules if results[name][0] != 0]
    for name in modules:
        returncode, elapsed, _stdout, _stderr = results[name]
        status = "pass" if returncode == 0 else "fail"
        print(f"{name}: {status} ({elapsed:.2f}s)")

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
run("git", "diff", "--check")
check_committed_delta()

# --- Project checks (TODO: pick one per your stack or replace as needed) ---

# Flutter / Dart:
# run("dart", "format", "--output=none", "--set-exit-if-changed", ".")
# run("flutter", "analyze")
# run("flutter", "test")

# Node / TypeScript:
# run("npx", "prettier", "--check", ".")
# run("npx", "eslint", ".")
# run("npm", "test")

# Python:
# run("ruff", "check", ".")
# run("ruff", "format", "--check", ".")
# run("pytest")
# run_unittest_parallel()

print("verify: OK")
