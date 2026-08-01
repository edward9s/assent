"""The ``shared-paths`` command: the only sanctioned writer of the local manifest.

``assent shared-paths review`` is a deliberately narrow operation.  It takes
either repeated ``--path`` values or an explicit ``--none``, plus the exact
``--watch`` files that say when the decision must be reconsidered, validates
every one of them against the primary worktree and the source snapshot, and only
then records a reviewed profile and reconciles this worktree's links.

There is no arbitrary target, no copy fallback, no glob, no "link every ignored
entry", no ``--force``, no secret-file mode and no Git staging or commit mode.
A scheduled AI session may not write into the primary worktree by hand; running
this validated operation is the one way it can settle the shared-path contract.

The parser is built here so the surrounding CLI can attach it, and the module
also runs standalone (``python -m assent.main shared-paths review ...``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from assent import AssentError, gitops, shared_paths

COMMAND = "shared-paths"


def add_shared_paths_command(sub: argparse._SubParsersAction) -> None:
    """Attach ``shared-paths`` and its single ``review`` operation to a CLI."""
    parser = sub.add_parser(
        COMMAND,
        help="review the shared ignored directories this project needs")
    operations = parser.add_subparsers(dest="operation", required=True)
    review = operations.add_parser(
        "review",
        help="record the reviewed shared directories for this source snapshot")
    review.add_argument(
        "--path", action="append", default=[], metavar="DIR",
        help="a project-relative ignored directory this project needs shared "
             "(repeatable)")
    review.add_argument(
        "--none", action="store_true",
        help="record that this snapshot needs no shared directory at all")
    review.add_argument(
        "--watch", action="append", default=[], metavar="FILE",
        help="a tracked dependency or build file whose change makes this "
             "decision worth reconsidering (repeatable, required)")


def build_parser() -> argparse.ArgumentParser:
    """The standalone parser, so the operation is runnable on its own."""
    parser = argparse.ArgumentParser(
        prog="assent", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    add_shared_paths_command(sub)
    return parser


def _primary_worktree(cwd: Path) -> Path:
    """The repository's main worktree, which is where the manifest always lives."""
    return gitops.main_worktree(cwd)


def shared_paths_review(paths: list[str], watch: list[str], none: bool,
                        cwd: Path | None = None) -> int:
    """Run one validated review from the current working tree.

    The worktree the command runs in supplies the source snapshot the profile is
    fingerprinted against and is the tree whose links are reconciled; the primary
    worktree supplies the link targets and holds the manifest.  Running it in the
    primary worktree itself caches the profile and links nothing.
    """
    here = Path.cwd() if cwd is None else Path(cwd)
    main = _primary_worktree(here)
    contract = shared_paths.review(
        main, here, paths=paths, watch=watch, none=none)
    print(f"Recorded shared-path profile {contract.profile.fingerprint[:12]} "
          f"in {shared_paths.manifest_path(main)}")
    print(shared_paths.describe(contract))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return shared_paths_review(args.path, args.watch, args.none)
    except AssentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":              # pragma: no cover - process entry point
    raise SystemExit(main())
