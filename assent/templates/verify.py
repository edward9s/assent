#!/usr/bin/env python3
"""Shared verification script: a task is not complete unless the verify command exits 0.

A task file's verify field points to this script by default (python .assent/verify.py);
individual tasks may swap in a faster or stricter command.
TODO: replace the "project checks" examples below with your project's actual check commands.
"""

import subprocess
import sys
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

print("verify: OK")
