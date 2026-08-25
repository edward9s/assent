"""Inspect or declare the project's required ignored directories.

``assent ignored-dirs declare`` is a deliberately narrow operation. It takes
either repeated ``--required`` values or ``--none-required``, plus the exact
``--watch`` files that say when the decision must be reconsidered, validates
every one of them against the primary worktree and the source snapshot, and only
then records a reviewed profile and reconciles this worktree's links.

``assent ignored-dirs status`` is the read-only companion.  It classifies the
worktree the command runs in, reports the matching profile and link agreement,
and never provisions a path or writes the primary worktree's local manifest.

There is no arbitrary target, no copy fallback, no glob, no "link every ignored
entry", no ``--force``, no secret-file mode and no Git staging or commit mode.
A scheduled AI session may not write into the primary worktree by hand; running
this validated operation is the one way it can settle the ignored-directory
decision.

The parser is built here so the surrounding CLI can attach it, and the module
also runs standalone
(``python -m assent.ignored_dirs_cli ignored-dirs declare ...``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from assent import AssentError, gitops, ignored_dirs

COMMAND = "ignored-dirs"
_DECLARE_HELP = """\
This is an Assent AI workflow operation, not a human recovery command. The
active source role runs it after reviewing an UNKNOWN or STALE decision. Human
recovery uses `assent rework`; the next run gives the decision back to the AI.
The operation runs only in the managed source worktree whose snapshot the
declaration describes.

Examples:
  assent ignored-dirs declare --required assets --not-required build "build output" --watch package.lock
  assent ignored-dirs declare --none-required --not-required build "build output" --watch package.lock

--required names a project-relative ignored directory that the source requires.
--not-required accounts for every inventory directory that is not required,
with a reason.
--watch is a repeatable, tracked dependency or build file; changing it makes the
decision stale. Every inventory directory must be covered exactly once by
--required or --not-required; either may cover a subtree. State one or more
--required values or --none-required, and always state at least one --watch file.
"""


def add_ignored_dirs_command(sub: argparse._SubParsersAction) -> None:
    """Attach the read-only status and controlled declaration operations."""
    parser = sub.add_parser(
        COMMAND,
        help="inspect or declare this source's required ignored directories")
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser(
        "status",
        help="show this worktree's ignored-directory decision and link state",
        description="Inspect this worktree's ignored-directory state without changing it.")
    declare = operations.add_parser(
        "declare",
        help="AI workflow: declare this source snapshot's required directories",
        description="Validate, record, and apply one ignored-directory decision.",
        epilog=_DECLARE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    declare.add_argument(
        "--required", action="append", default=[], metavar="DIR",
        help="a project-relative ignored directory this source requires "
             "(repeatable)")
    declare.add_argument(
        "--none-required", action="store_true",
        help="record that this source requires no ignored directory")
    declare.add_argument(
        "--not-required", action="append", nargs=2, default=[],
        metavar=("DIR", "REASON"),
        help="record why one ignored inventory directory is not required "
             "(repeatable)")
    declare.add_argument(
        "--watch", action="append", default=[], metavar="FILE",
        help="a tracked dependency or build file whose change makes this "
             "decision worth reconsidering (repeatable, required)")


def build_parser() -> argparse.ArgumentParser:
    """The standalone parser, so the operation is runnable on its own."""
    parser = argparse.ArgumentParser(
        prog="assent", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add_ignored_dirs_command(sub)
    return parser


def _primary_worktree(cwd: Path) -> Path:
    """The repository's main worktree, which is where the manifest always lives."""
    return gitops.main_worktree(cwd)


def ignored_dirs_declare(required: list[str], watch: list[str],
                         none_required: bool,
                         not_required: list[list[str]] | None = None,
                         cwd: Path | None = None) -> int:
    """Submit one validated declaration from the current working tree.

    The worktree the command runs in supplies the source snapshot the profile is
    fingerprinted against and is the tree whose links are reconciled; the primary
    worktree supplies the link targets and holds the manifest.
    """
    here = Path.cwd() if cwd is None else Path(cwd)
    main = _primary_worktree(here)
    if here.resolve() == main.resolve():
        raise AssentError(
            "ignored-dirs declare is an AI workflow operation and must run "
            "in its managed source worktree")
    decision = ignored_dirs.declare(
        main, here, required=required, watch=watch,
        none_required=none_required,
        not_required=tuple(
            ignored_dirs.NonRequiredDirectory(path, reason)
            for path, reason in (not_required or [])))
    print(f"Recorded ignored-directory profile {decision.profile.fingerprint[:12]} "
          f"in {ignored_dirs.manifest_path(main)}")
    print(ignored_dirs.describe(decision))
    return 0


def ignored_dirs_status(cwd: Path | None = None) -> int:
    """Describe the current worktree's ignored-directory decision without changing it."""
    here = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    main = _primary_worktree(here).resolve()
    manifest = ignored_dirs.read_manifest(main)
    decision = ignored_dirs.classify(main, here, manifest)

    print(f"Current worktree: {here}")
    print(f"Primary worktree: {main}")
    presence = "present" if manifest.present else "absent"
    print(f"Manifest: {ignored_dirs.manifest_path(main)} ({presence})")
    print(f"State: {decision.state}")
    if decision.profile is None:
        print("Profile: none")
    else:
        print(f"Profile: {decision.profile.fingerprint}")
        print("Required directories: "
              + (", ".join(decision.profile.required) or "none"))
        print("Watch files: " + (", ".join(decision.profile.watch) or "none"))
    if decision.prior_required and decision.profile is None:
        print("Previously required directories: "
              + ", ".join(decision.prior_required))
    if decision.evidence:
        print("Evidence:")
        for item in decision.evidence:
            print(f"  - {item}")

    if here == main:
        print("Links: not applicable (the primary worktree contains the targets)")
        return 0
    if not decision.settled:
        print("Links: not evaluated until a declaration settles this decision")
        return 0
    try:
        ignored_dirs.require_directory_link_agreement(main, here, decision)
    except AssentError as e:
        print("Links: INVALID")
        print(f"Problem: {e}")
        return 1
    print("Links: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.operation == "status":
            return ignored_dirs_status()
        return ignored_dirs_declare(
            args.required, args.watch, args.none_required, args.not_required)
    except AssentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":              # pragma: no cover - process entry point
    raise SystemExit(main())
