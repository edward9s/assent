"""Test-only helpers for creating and removing directory links safely."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from assent import AssentError, pathops


def make_directory_link(link: Path, target: Path) -> None:
    """Create the real directory-link kind used by the platform under test."""
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def detach_directory_links(root: Path) -> None:
    """Detach every directory link below ``root`` without opening its target."""
    for link in pathops.inventory_directory_links(root):
        pathops.detach_directory_link(link)


def safe_rmtree(path: Path) -> None:
    """Remove a test tree only after its directory links have been detached.

    A failed teardown must leak its temporary directory rather than recursively
    walk through an unresolved link and destroy the target used by another test.
    """
    path = Path(path)
    if not os.path.lexists(path):
        return
    try:
        if pathops.is_link(path):
            if os.path.isdir(path):
                pathops.detach_directory_link(path)
            else:
                path.unlink()
            return
        detach_directory_links(path)
    except (AssentError, OSError):
        return
    shutil.rmtree(path, ignore_errors=True)


def cleanup_worktree(root: Path, path: Path) -> None:
    """Force-remove a test worktree only after its links are detached."""
    path = Path(path)
    if os.path.lexists(path):
        try:
            detach_directory_links(path)
        except (AssentError, OSError):
            return
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(path)],
        cwd=root, capture_output=True)
    if os.path.lexists(path):
        safe_rmtree(path)

