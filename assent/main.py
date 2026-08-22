"""Inspect or declare the project's shared ignored directories.

``assent shared-paths declare`` is a deliberately narrow operation.  It takes
either repeated ``--path`` values or an explicit ``--none``, plus the exact
``--watch`` files that say when the decision must be reconsidered, validates
every one of them against the primary worktree and the source snapshot, and only
then records a reviewed profile and reconciles this worktree's links.

``assent shared-paths status`` is the read-only companion.  It classifies the
worktree the command runs in, reports the matching profile and link agreement,
and never provisions a path or writes the primary worktree's local manifest.

There is no arbitrary target, no copy fallback, no glob, no "link every ignored
entry", no ``--force``, no secret-file mode and no Git staging or commit mode.
A scheduled AI session may not write into the primary worktree by hand; running
this validated operation is the one way it can settle the shared-path contract.

The parser is built here so the surrounding CLI can attach it, and the module
also runs standalone (``python -m assent.main shared-paths declare ...``).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from assent import AssentError, gitops, shared_paths

COMMAND = "shared-paths"
_DECLARE_HELP = """\
Normally the active Assent AI session runs this after reviewing an UNKNOWN or
STALE shared-path decision. Run it in the managed source worktree whose snapshot
the declaration describes. Running it in the primary worktree records a profile
for that primary snapshot but creates no links there.

Examples:
  assent shared-paths declare --path assets --classify build "build output" --watch package.lock
  assent shared-paths declare --none --classify build "build output" --watch package.lock

--path is a repeatable project-relative ignored directory that compilation or
testing requires. --classify accounts for every inventory directory that is not
shared, with a reason.
--watch is a repeatable, tracked dependency or build file; changing it makes the
decision stale. Every inventory directory must be covered exactly once by --path or
--classify; either may cover a subtree. State one or more --path values or
--none, and always state at least one --watch file.
"""


def add_shared_paths_command(sub: argparse._SubParsersAction) -> None:
    """Attach the read-only status and controlled declaration operations."""
    parser = sub.add_parser(
        COMMAND,
        help="inspect or declare the shared ignored directories this project needs")
    operations = parser.add_subparsers(dest="operation", required=True)
    operations.add_parser(
        "status",
        help="show this worktree's shared-path decision and link state",
        description="Inspect this worktree's shared-path state without changing it.")
    declare = operations.add_parser(
        "declare",
        help="declare the shared directories required by this source snapshot",
        description="Validate, record, and apply one shared-directory declaration.",
        epilog=_DECLARE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    declare.add_argument(
        "--path", action="append", default=[], metavar="DIR",
        help="a project-relative ignored directory this project needs shared "
             "(repeatable)")
    declare.add_argument(
        "--none", action="store_true",
        help="record that this snapshot needs no shared directory at all")
    declare.add_argument(
        "--classify", action="append", nargs=2, default=[],
        metavar=("PATH", "REASON"),
        help="record why one ignored inventory directory is not shared "
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
    add_shared_paths_command(sub)
    return parser


def _primary_worktree(cwd: Path) -> Path:
    """The repository's main worktree, which is where the manifest always lives."""
    return gitops.main_worktree(cwd)


def shared_paths_declare(paths: list[str], watch: list[str], none: bool,
                         classifications: list[list[str]] | None = None,
                         cwd: Path | None = None) -> int:
    """Submit one validated declaration from the current working tree.

    The worktree the command runs in supplies the source snapshot the profile is
    fingerprinted against and is the tree whose links are reconciled; the primary
    worktree supplies the link targets and holds the manifest.  Running it in the
    primary worktree itself caches the profile and links nothing.
    """
    here = Path.cwd() if cwd is None else Path(cwd)
    main = _primary_worktree(here)
    contract = shared_paths.declare(
        main, here, paths=paths, watch=watch, none=none,
        dispositions=tuple(
            shared_paths.PathDisposition(path, reason)
            for path, reason in (classifications or [])))
    print(f"Recorded shared-path profile {contract.profile.fingerprint[:12]} "
          f"in {shared_paths.manifest_path(main)}")
    print(shared_paths.describe(contract))
    return 0


def shared_paths_status(cwd: Path | None = None) -> int:
    """Describe the current worktree's shared-path contract without changing it."""
    here = (Path.cwd() if cwd is None else Path(cwd)).resolve()
    main = _primary_worktree(here).resolve()
    manifest = shared_paths.read_manifest(main)
    contract = shared_paths.classify(main, here, manifest)

    print(f"Current worktree: {here}")
    print(f"Primary worktree: {main}")
    presence = "present" if manifest.present else "absent"
    print(f"Manifest: {shared_paths.manifest_path(main)} ({presence})")
    print(f"State: {contract.state}")
    if contract.profile is None:
        print("Profile: none")
    else:
        print(f"Profile: {contract.profile.fingerprint}")
        print("Shared paths: " + (", ".join(contract.profile.paths) or "none"))
        print("Watch files: " + (", ".join(contract.profile.watch) or "none"))
    if contract.prior_paths and contract.profile is None:
        print("Previously reviewed paths: " + ", ".join(contract.prior_paths))
    if contract.evidence:
        print("Evidence:")
        for item in contract.evidence:
            print(f"  - {item}")

    if here == main:
        print("Links: not applicable (the primary worktree contains the targets)")
        return 0
    if not contract.settled:
        print("Links: not evaluated until a declaration settles this decision")
        return 0
    try:
        shared_paths.require_directory_link_agreement(main, here, contract)
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
            return shared_paths_status()
        return shared_paths_declare(
            args.path, args.watch, args.none, args.classify)
    except AssentError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":              # pragma: no cover - process entry point
    raise SystemExit(main())
