"""Filesystem primitives for acting on a worktree without following a link.

Two modules need the same two facts about a path: whether it is a link that
Git or Python would happily walk through, and how to delete that link object
without touching whatever it points at.  ``assent.gitops`` needs them to empty
a worktree of links before Git's own recursive removal starts, and
``assent.verification_common`` needs them to mirror and unmirror a candidate's
provisioned artifacts.  Keeping the answer here, below both of them, gives
link safety exactly one definition and leaves the two modules independent.

Nothing in this module enumerates, modifies, or deletes anything through a
link target.  A path it cannot classify is refused with the path named rather
than resolved into a wider target.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

from assent import AssentError

# ``st_reparse_tag`` and ``st_file_attributes`` exist only on Windows, and a
# junction is the one reparse point Python does not report as a symlink, so
# both lookups are guarded rather than gated on a platform test.
_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


def is_link_stat(info: os.stat_result) -> bool:
    """True when ``lstat``-style metadata describes a link object.

    ``stat.S_ISLNK`` covers a POSIX symlink and a Windows file or directory
    symlink; a Windows junction is not a symlink to Python (``os.path.islink``
    returns False for one), so its reparse tag is checked as well.
    """
    if stat.S_ISLNK(info.st_mode):
        return True
    return (_MOUNT_POINT_TAG is not None
            and getattr(info, "st_reparse_tag", None) == _MOUNT_POINT_TAG)


def is_link(path: Path | str) -> bool:
    """True when ``path`` itself is a link; an unreadable path is not one."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return is_link_stat(info)


def is_reparse_point(info: os.stat_result) -> bool:
    """True for any Windows reparse point, including tags assent cannot classify."""
    return bool(getattr(info, "st_file_attributes", 0)
                & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _links_to_directory(path: Path, info: os.stat_result) -> bool:
    """True when a link object occupies the place of a directory.

    On Windows the answer is in the link's own attributes, so a junction or a
    directory symlink is recognized without opening the target at all.  A POSIX
    symlink carries no such attribute, so the target's kind is read with a
    single ``stat``; that is a metadata question, never an enumeration.
    """
    if os.name == "nt":
        return bool(getattr(info, "st_file_attributes", 0)
                    & stat.FILE_ATTRIBUTE_DIRECTORY)
    return os.path.isdir(path)


def detach_directory_link(path: Path) -> None:
    """Delete one directory link object; the target it points at is never touched.

    A Windows junction and a Windows directory symlink are directory entries
    that ``rmdir`` removes, and a POSIX directory symlink is an ordinary link
    that ``unlink`` removes.  Neither call descends into the target.  ``OSError``
    is deliberately left unwrapped so each caller can phrase its own refusal.
    """
    if os.name == "nt":
        os.rmdir(path)
    else:
        os.unlink(path)


def create_directory_link(destination: Path, target: Path) -> None:
    """Create one directory link at ``destination`` pointing at ``target``.

    A Windows directory symlink needs a privilege an unattended run cannot
    assume, while a junction needs none, so Windows always gets a junction and
    POSIX a directory symlink.  Both the persistent provisioning a source
    worktree keeps and the temporary mirroring a verification candidate uses go
    through this one primitive, so "how assent makes a directory link" has a
    single definition beside ``detach_directory_link``, which undoes it without
    traversing the target.  ``OSError`` is left unwrapped so each caller can
    phrase its own refusal.
    """
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(destination))
    else:
        os.symlink(target, destination, target_is_directory=True)


def _require_inside(root: Path, path: Path) -> Path:
    """Refuse a path that is not lexically beneath the exact worktree root."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise AssentError(
            f"refusing to act on {path} while inventorying the worktree "
            f"{root}: it is not inside that worktree") from None
    parts = relative.parts
    if not parts or any(part in ("", os.curdir, os.pardir) for part in parts):
        raise AssentError(
            f"refusing to act on {path} while inventorying the worktree "
            f"{root}: {'/'.join(parts) or path.name!r} is not a safe relative path")
    return path


def inventory_directory_links(root: Path) -> tuple[Path, ...]:
    """List every directory link inside an owned worktree, without entering one.

    Traversal descends into ordinary directories only: a link and a directory
    reparse point are classified where they are found and never opened, so
    nothing outside ``root`` is ever enumerated.  Every path acted on is checked
    to be lexically beneath ``root``, so an odd name can never widen the
    deletion target, and the root itself is refused when it is a link rather
    than resolved into whatever it points at.

    A missing ``root`` has nothing to detach and yields an empty inventory,
    which is what makes a rerun after an interrupted or partial cleanup safe:
    an already-detached link is simply absent from the next inventory.
    """
    root = Path(root)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return ()
    except OSError as e:
        raise AssentError(
            f"unable to inspect the worktree root {root} before removing it: "
            f"{e}") from e
    if is_link_stat(info) or is_reparse_point(info):
        raise AssentError(
            f"refusing to remove the worktree {root}: its root is itself a link "
            "or reparse point, and removing it would act on an external target")
    if not stat.S_ISDIR(info.st_mode):
        raise AssentError(
            f"refusing to remove the worktree {root}: it is not a directory")

    links: list[Path] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                names = [entry.name for entry in entries]
        except OSError as e:
            raise AssentError(
                f"unable to read {current} while inventorying the worktree "
                f"{root} for links: {e}") from e
        for name in names:
            path = _require_inside(root, current / name)
            try:
                child = os.lstat(path)
            except OSError as e:
                raise AssentError(
                    f"unable to inspect {path} while inventorying the worktree "
                    f"{root} for links: {e}") from e
            if is_link_stat(child):
                if _links_to_directory(path, child):
                    links.append(path)
                continue                    # a link is classified, never walked
            if stat.S_ISDIR(child.st_mode):
                if is_reparse_point(child):
                    raise AssentError(
                        f"refusing to remove the worktree {root}: {path} is a "
                        "directory reparse point assent cannot classify, so it "
                        "is left in place rather than walked into")
                pending.append(path)
    return tuple(sorted(links))
