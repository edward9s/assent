"""Fake ``agy`` CLI used only by the hermetic antigravity reliability tests.

A real subprocess, never a mock: it is spawned with ``sys.executable`` in front of it (a
bare ``.py`` file is not directly executable on Windows), so it exercises the exact
argv-list, no-shell, real stdout/stderr, real process-kill mechanics that
``assent.adapters.claude.run_subprocess`` implements for the real ``agy`` binary. It never
touches the network, a real model or a real login; the outcome is chosen purely by the
``FAKE_AGY_SCENARIO`` environment variable, so no test depends on model or vendor ordering.

Flags mirror the real CLI's headless shape closely enough for a round-trip check
(``--print``, ``--model``, ``--effort``, ``--mode``, ``--print-timeout``, ``--log-file``,
``--add-dir``); anything else is accepted and ignored via ``parse_known_args`` so a future
adapter flag never breaks this fixture.
"""
import argparse
import os
import sys
import time

# The real agy CLI is trusted to write UTF-8 regardless of the host console's codepage (the
# antigravity adapter's own module docstring documents this as a shipped requirement); this
# fixture must match that or a Traditional-Chinese/emoji prompt would round-trip as mojibake
# purely as an artifact of this test double, not of anything assent itself does.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print", dest="prompt", required=True)
    parser.add_argument("--model")
    parser.add_argument("--effort")
    parser.add_argument("--mode")
    parser.add_argument("--print-timeout")
    parser.add_argument("--log-file")
    parser.add_argument("--add-dir", action="append", default=[])
    known, _ = parser.parse_known_args(argv)
    return known


def _heartbeat(path: str | None) -> None:
    """Advance the log file's mtime only; the content is never meaningful, only the
    timestamp is ever read back by assent's watchdog."""
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(".")


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    scenario = os.environ.get("FAKE_AGY_SCENARIO", "success")

    pidfile = os.environ.get("FAKE_AGY_PIDFILE")
    if pidfile:
        with open(pidfile, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))

    if scenario == "success":
        print("Done. The task finished successfully.")
        return 0

    if scenario == "success_with_quota_word":
        # An ordinary, successful answer that happens to say "quota"/"limit": exit 0 text
        # containing these words must never be misread as a quota failure.
        print("Your current plan has plenty of quota remaining; no rate limit was hit.")
        return 0

    if scenario == "stderr_error":
        print("Error: transport closed unexpectedly", file=sys.stderr)
        return 1

    if scenario == "permission":
        print("Error: permission denied for tool write_to_file", file=sys.stderr)
        return 1

    if scenario == "quota":
        print("Error: Resource has been exhausted (e.g. check quota).", file=sys.stderr)
        return 1

    if scenario == "unsupported_model":
        print(f'Error: invalid model selection (--model "{args.model}" '
              f'--effort "{args.effort}"): {args.model} has no "{args.effort}" effort',
              file=sys.stderr)
        return 1

    if scenario == "print_timeout":
        print("Error: print-timeout exceeded while waiting for a response",
              file=sys.stderr)
        return 1

    if scenario == "echo_roundtrip":
        # Proves the prompt and --add-dir values survived the no-shell argv list exactly:
        # Unicode, embedded whitespace, and embedded newlines all included.
        print(f"PROMPT_LEN={len(args.prompt)}")
        print("PROMPT_REPR=" + repr(args.prompt))
        print("ADD_DIRS=" + repr(args.add_dir))
        return 0

    if scenario == "streaming":
        for i in range(3):
            print(f"line {i}", flush=True)
            time.sleep(float(os.environ.get("FAKE_AGY_TICK_SECONDS", "0.1")))
        return 0

    if scenario == "silent_alive":
        # A print-mode session that only prints once, at the very end, but keeps updating
        # its own log file the whole time it is working: the watchdog must read that as
        # activity and must not kill it.
        ticks = int(os.environ.get("FAKE_AGY_TICKS", "6"))
        interval = float(os.environ.get("FAKE_AGY_TICK_SECONDS", "0.1"))
        for _ in range(ticks):
            time.sleep(interval)
            _heartbeat(args.log_file)
        print("Done after a long silent-but-alive stretch.")
        return 0

    if scenario in ("silent_dead", "hang"):
        # Genuinely stuck: no stdout, no log heartbeat. The watchdog (or, for "hang", a
        # simulated Ctrl-C) must be the only thing that ever ends this process.
        time.sleep(float(os.environ.get("FAKE_AGY_HANG_SECONDS", "30")))
        print("should never be seen", file=sys.stderr)
        return 0

    print(f"Error: unknown FAKE_AGY_SCENARIO {scenario!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
