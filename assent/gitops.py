"""Git operations: worktree, branches, clean/scope checks, commit, restore.

All Git commands run with cwd=project root, and the return code is checked before use;
a missing git binary gets a clear error message instead of a traceback. `excludes` is a
list of relative paths for runtime artifacts (_assent.log, _report.md, etc.): they are
never input or checkpoint content, so they never take part in clean checks, scope checks,
or commits.
"""
from __future__ import annotations

import contextlib
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from assent import AssentError


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess:
    # core.quotepath=false: by default Git octal-escapes non-ASCII filenames (Chinese, etc.)
    # into pure ASCII (e.g. "\346\270\254\350\251\246.txt"); turning it off prints the raw
    # UTF-8 filename instead.
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args], cwd=root,
            capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise AssentError("git executable not found; confirm git is installed and on PATH") from e


def _git(root: Path, *args: str) -> str:
    result = _run_git(root, *args)
    if result.returncode != 0:
        raise AssentError(
            f"git {' '.join(args)} failed (exit code {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def _describe_change(line: str) -> str:
    """Translate one ``git status --porcelain`` line into a human-readable "status: path"."""
    code = line[:2]
    path = line[3:].strip().strip('"')
    if " -> " in path:                       # rename: "old -> new", keep new
        path = path.split(" -> ", 1)[1].strip().strip('"')
    if code == "??":
        label = "untracked (new file)"
    elif "R" in code:
        label = "renamed"
    elif "A" in code:
        label = "staged"
    elif "D" in code:
        label = "deleted"
    elif "M" in code:
        label = "modified"
    else:
        label = f"change ({code.strip() or code})"
    return f"{label}: {path}"


def _normalize(path_str: str) -> str:
    return path_str.replace("\\", "/")


def _status_path(line: str) -> str:
    path = line[3:].strip().strip('"')
    if " -> " in path:
        path = path.split(" -> ", 1)[1].strip().strip('"')
    return _normalize(path)


def _meaningful_status_lines(output: str, excludes: Sequence[str]) -> list[str]:
    excluded = {_normalize(e) for e in excludes}
    return [line for line in output.splitlines()
            if line.strip() and _status_path(line) not in excluded]


def ensure_clean(root: Path, excludes: Sequence[str] = ()) -> None:
    """Working tree is not clean (including untracked files) -> raise AssentError."""
    out = _git(root, "status", "--porcelain")
    lines = _meaningful_status_lines(out, excludes)
    if lines:
        detail = "\n".join(f"  - {_describe_change(ln)}" for ln in lines)
        raise AssentError(
            # clean.py matches the leading "Working tree is not clean" substring
            # to recognise this as a cleanliness exception; keep that prefix intact.
            "Working tree is not clean, cannot continue (please commit "
            f"these changes first, or add files that should not be tracked to "
            f".gitignore):\n{detail}")


def ensure_branch(root: Path, prefix: str) -> str:
    """Reuse the current branch if already on <prefix>; otherwise create <prefix><UTC timestamp>."""
    current = _git(root, "branch", "--show-current").strip()
    if current.startswith(prefix):
        return current
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    branch = f"{prefix}{run_id}"
    _git(root, "checkout", "-b", branch)
    return branch


def worktree_path(root: Path, folder: str) -> Path:
    """Return the fixed worktree path for the given task folder."""
    return root.parent / f"{root.name}.worktrees" / folder


def _resolved_git_path(root: Path, value: str) -> Path:
    """Normalize an absolute or relative path returned by git into a comparable absolute path."""
    path = Path(value.strip())
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _is_repo_worktree(root: Path, path: Path) -> bool:
    """Determine whether path is a valid top-level worktree of the repo owning root."""
    top = _run_git(path, "rev-parse", "--show-toplevel")
    common = _run_git(path, "rev-parse", "--git-common-dir")
    if top.returncode != 0 or common.returncode != 0:
        return False

    root_common = _git(root, "rev-parse", "--git-common-dir")
    return (_resolved_git_path(path, top.stdout) == path.resolve()
            and _resolved_git_path(path, common.stdout)
            == _resolved_git_path(root, root_common))


def ensure_worktree(root: Path, folder: str,
                    start_snapshot: str | None = None) -> Path:
    """Create or reuse a fixed-path, detached-HEAD git worktree.

    ``start_snapshot`` is resolved to an exact commit and is used only when
    the worktree is first created.  Reusing an existing worktree never moves
    its HEAD or changes its branch, even when a different snapshot is passed.
    """
    root = root.resolve()
    path = worktree_path(root, folder)
    if path.exists():
        if path.is_dir() and _is_repo_worktree(root, path):
            return path
        raise AssentError(
            f"worktree path already exists but is not a valid worktree of this repo: {path}")

    # If the path was manually deleted, clear stale worktree metadata still held by the main repo.
    _git(root, "worktree", "prune")
    args = ["worktree", "add", "--detach", str(path)]
    if start_snapshot is not None:
        args.append(_commit_snapshot(root, start_snapshot))
    _git(root, *args)
    return path


def cleanup_unstarted_worktree(root: Path, folder: str,
                               expected_tip: str,
                               branch_prefix: str) -> None:
    """Remove a newly created, still-unused folder worktree conservatively.

    This is exclusively for setup failures before an AI session starts.  The
    exact path, clean state, HEAD and branch ownership must all be provable.
    Failure leaves the path/ref in place as recovery evidence rather than
    widening cleanup or deleting uncertain resources.
    """
    primary = main_worktree(Path(root).resolve())
    path = worktree_path(primary, folder)
    snapshot = _commit_snapshot(primary, expected_tip)
    if not path.exists():
        return
    if not path.is_dir() or not _is_repo_worktree(primary, path):
        raise AssentError(
            f"new worktree cleanup refused; recoverable path retained: {path}")

    branch = current_branch(path)
    head = commit_of(path, "HEAD")
    if head != snapshot:
        raise AssentError(
            f"new worktree cleanup refused because HEAD moved from {snapshot} "
            f"to {head}; recoverable path retained: {path}")
    if branch and not branch.startswith(branch_prefix):
        raise AssentError(
            f"new worktree cleanup refused for foreign branch {branch}; "
            f"recoverable path retained: {path}")
    if not working_tree_status(path).is_clean:
        raise AssentError(
            f"new worktree cleanup refused because it is dirty; "
            f"recoverable path retained: {path}")

    removed = _run_git(primary, "worktree", "remove", str(path))
    if removed.returncode != 0:
        raise AssentError(
            "new worktree cleanup was incomplete; recoverable path/ref retained: "
            f"{path}{f', {branch}' if branch else ''} "
            f"({removed.stderr.strip() or removed.stdout.strip() or 'unknown error'})")

    if branch:
        current_tip = branch_tip(primary, branch)
        if current_tip != snapshot:
            raise AssentError(
                f"new worktree path was removed but branch {branch} moved to "
                f"{current_tip}; recoverable ref retained")
        deleted = _run_git(primary, "branch", "-D", branch)
        if deleted.returncode != 0:
            raise AssentError(
                f"new worktree path was removed but recoverable ref {branch} "
                "was retained because branch cleanup failed: "
                f"{deleted.stderr.strip() or deleted.stdout.strip() or 'unknown error'}")


def is_repo_worktree(root: Path, path: Path) -> bool:
    """Publicly query whether ``path`` is a valid top-level worktree of the ``root`` repo."""
    return path.is_dir() and _is_repo_worktree(root.resolve(), path.resolve())


def branches_with_prefix(root: Path, prefix: str) -> list[str]:
    """List local ``refs/heads`` branches with the given prefix, sorted by name."""
    # Do not pass prefix straight to Git as a ref pattern: task folder names already forbid
    # path separators but may still contain *, ?, [ — wildcards must not widen cleanup scope.
    out = _git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return sorted(branch for line in out.splitlines()
                  if (branch := line.strip()).startswith(prefix))


def current_branch(root: Path) -> str:
    """Return the current branch; detached HEAD returns an empty string."""
    return _git(root, "branch", "--show-current").strip()


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    """Determine whether ``ancestor`` is an ancestor of ``descendant``; query errors fail loudly."""
    result = _run_git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise AssentError(
        "git merge-base --is-ancestor "
        f"{ancestor} {descendant} failed (exit code {result.returncode}): "
        f"{result.stderr.strip() or result.stdout.strip()}")


def merge_base(root: Path, first: str, second: str) -> str:
    """Return the unique best common ancestor of two commits."""
    bases = [line.strip() for line in _git(
        root, "merge-base", "--all", first, second).splitlines()
             if line.strip()]
    if len(bases) != 1:
        detail = ", ".join(bases) if bases else "none"
        raise AssentError(
            f"git merge-base found {len(bases)} best common ancestors for "
            f"{first} and {second}: {detail}")
    return bases[0]


def remove_worktree(root: Path, path: Path) -> None:
    """Remove a worktree using Git's ordinary safeguards; deliberately no force option."""
    _git(root, "worktree", "remove", str(path))


def delete_branch(root: Path, branch: str) -> None:
    """Delete an already-merged branch with ``git branch -d``, keeping Git's second safeguard."""
    _git(root, "branch", "-d", branch)


def delete_branch_force(root: Path, branch: str) -> None:
    """Force-delete a branch with ``git branch -D``; for manual reject decisions only.

    Callers must first record the full tip hash via :func:`commit_of` for durable evidence;
    after deletion the hash can still recover the branch within the gc grace period. Routine
    cleanup always goes through :func:`delete_branch`.
    """
    _git(root, "branch", "-D", branch)


def commit_of(root: Path, ref: str) -> str:
    """Return the full commit hash that ``ref`` points to, for durable evidence before deletion."""
    return _git(root, "rev-parse", ref).strip()


def commit_message(root: Path, ref: str = "HEAD") -> str:
    """Return the full message of the given commit, to identify a resumable, durable checkpoint."""
    return _git(root, "show", "-s", "--format=%B", ref).rstrip("\r\n")


def commit_history(
        root: Path, ref: str = "HEAD",
) -> list[tuple[str, tuple[str, ...], str]]:
    """List commit hash, parents, and subject along first-parent history; fail closed if unparseable."""
    out = _git(root, "log", "--first-parent", "-z",
               "--format=%H%x00%P%x00%s", ref)
    values = out.split("\0")
    if values and values[-1] == "":
        values.pop()
    if len(values) % 3:
        raise AssentError("unable to parse Git history format")

    history: list[tuple[str, tuple[str, ...], str]] = []
    for index in range(0, len(values), 3):
        commit = values[index].strip()
        parents = tuple(values[index + 1].split())
        subject = values[index + 2]
        if not commit or "\n" in commit or "\r" in commit:
            raise AssentError("unable to parse Git history format")
        history.append((commit, parents, subject))
    return history


def revert_no_commit(root: Path, commits: Sequence[str]) -> None:
    """Reverse-apply commits in input order, without creating a commit yet."""
    if not commits:
        raise AssentError("no commits to revert")
    _git(root, "revert", "--no-commit", *commits)


def abort_revert(root: Path) -> None:
    """Abort an in-progress git revert, returning to the state before it started."""
    _git(root, "revert", "--abort")


def tracked_paths(root: Path, path: str, ref: str | None = None) -> list[str]:
    """List indexed files under the given path; with a ref, query that commit/ref instead."""
    normalized = _normalize(path)
    if ref is None:
        out = _git(root, "ls-files", "--", normalized)
    else:
        result = _run_git(root, "ls-tree", "-r", "--name-only", ref,
                          "--", normalized)
        if result.returncode != 0:
            return []
        out = result.stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def changes_outside_scope(root: Path, scope: list[str],
                          since_ref: str | None = None,
                          excludes: Sequence[str] = ()) -> list[str]:
    """Return paths changed outside scope (current working tree + optionally committed changes).

    - When since_ref is given, committed changes between since_ref..HEAD (wip checkpoints
      created during quota interruption/retry) are also checked, so that after "keep progress,
      never restore" the scope check still covers every change made since the task's start.
    - An empty scope list fails closed: every change counts as out of scope. This is deliberate —
      scope exists to contain an unsupervised executing AI, and "unwritten = unrestricted" would
      let that protection quietly disappear. (Task-file parsing already forces scope to be
      non-empty; this fail-closed behavior here is the last line of defense.)
    """
    excluded = {_normalize(e) for e in excludes}
    paths: list[str] = []
    out = _git(root, "status", "--porcelain")
    for line in out.splitlines():
        if not line.strip():
            continue
        path_part = _status_path(line)
        if path_part not in excluded:
            paths.append(path_part)
    if since_ref:
        diff = _git(root, "diff", "--name-only", since_ref, "HEAD")
        paths += [p.strip().strip('"') for p in diff.splitlines()
                  if p.strip() and _normalize(p.strip().strip('"')) not in excluded]

    normalized_scope = [_normalize(s) for s in scope]
    outside: list[str] = []
    seen: set[str] = set()
    for path_part in paths:
        path_norm = _normalize(path_part)
        if path_norm in seen:
            continue
        seen.add(path_norm)
        if not any(path_norm == s or path_norm.startswith(s.rstrip("/") + "/")
                   for s in normalized_scope):
            outside.append(path_part)
    return outside


def _pathspec_excludes(root: Path, excludes: Sequence[str]) -> list[str]:
    """Filter the exclude list down to entries safe to write into a :(exclude) pathspec.

    Entries already covered by .gitignore (e.g. a project where the whole .assent/ is
    untracked) would never be added anyway, and naming an ignored path in a pathspec makes
    ``git add`` refuse with exit code 1 — check-ignore exit code 0 = ignored -> drop it;
    1 = not ignored, 128 = error -> keep it (keeping error cases is the conservative choice:
    if something is genuinely wrong, add fails loudly instead of silently swallowing it).
    """
    keep: list[str] = []
    for e in excludes:
        result = _run_git(root, "check-ignore", "-q", "--", e)
        if result.returncode != 0:
            keep.append(e)
    return keep


def _embedded_repo_paths(root: Path) -> list[str]:
    """Find embedded repos (marked by a .git entry) among the current working tree changes."""
    out = _git(root, "status", "--porcelain", "--untracked-files=all")
    paths: list[str] = []
    seen: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        path = _status_path(line).rstrip("/")
        if path in seen:
            continue
        candidate = root / path
        if candidate.is_dir() and (candidate / ".git").exists():
            seen.add(path)
            paths.append(path)
    return paths


def commit_all(root: Path, message: str, excludes: Sequence[str] = ()) -> None:
    """git add -A (excluding runtime artifacts and embedded repos) && git commit -m message."""
    embedded = _embedded_repo_paths(root)
    for path in embedded:
        print(f"warning: skipped embedded repo: {path}, handle it manually")
    all_excludes = [*excludes, *embedded]
    spec = ["--", "."] + [f":(exclude){e}"
                          for e in _pathspec_excludes(root, all_excludes)]
    _git(root, "add", "-A", *spec)
    _git(root, "commit", "-m", message)


def commit_if_dirty(root: Path, message: str, excludes: Sequence[str] = ()) -> bool:
    """Commit only if the working tree has any changes (including untracked); return whether it did.

    The engine uses this to preserve progress (wip checkpoints on quota interruption/user
    interruption): tokens have already been spent, so the output must never be discarded —
    a direct consequence of "minimizing token spend takes priority."
    """
    out = _git(root, "status", "--porcelain")
    if not _meaningful_status_lines(out, excludes):
        return False
    commit_all(root, message, excludes)
    return True


def head_ref(root: Path) -> str | None:
    """The current HEAD commit hash; returns None if unavailable (e.g. an empty repo)."""
    result = _run_git(root, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def restore(root: Path) -> None:
    """Discard all uncommitted working-tree changes: checkout -- . && clean -fd.

    Note: this deletes output the executing AI already spent tokens producing; the engine's
    normal flow never calls it. Reserved for manual rescue use only.
    """
    _git(root, "checkout", "--", ".")
    _git(root, "clean", "-fd")


# --- Local accept integration foundation ---


def main_worktree(root: Path) -> Path:
    """Return the main worktree from either it or one of its linked worktrees."""
    out = _git(root, "worktree", "list", "--porcelain")
    for line in out.splitlines():
        if line.startswith("worktree "):
            return Path(line.removeprefix("worktree ")).resolve()
    raise AssentError(
        "unable to determine the main worktree from `git worktree list` output")


def git_common_dir(root: Path) -> Path:
    """Return the repository-wide Git directory shared by all linked worktrees."""
    return _resolved_git_path(
        Path(root).resolve(), _git(Path(root), "rev-parse", "--git-common-dir"))


def require_current_branch(root: Path) -> str:
    """Return the current branch, refusing a detached integration target."""
    branch = current_branch(root)
    if not branch:
        raise AssentError(
            f"{root} is in detached HEAD state; check out a normal branch before "
            "integrating accepted work")
    return branch


@dataclass(frozen=True)
class WorkingTreeStatus:
    """Categorized porcelain status for an integration target."""

    staged: list[str]
    unstaged: list[str]
    untracked: list[str]

    @property
    def is_clean(self) -> bool:
        return not (self.staged or self.unstaged or self.untracked)


def working_tree_status(root: Path,
                        excludes: Sequence[str] = ()) -> WorkingTreeStatus:
    """Return staged, unstaged, and untracked paths separately."""
    excluded = {_normalize(value) for value in excludes}
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []
    for line in _git(root, "status", "--porcelain").splitlines():
        if not line.strip():
            continue
        path = _status_path(line)
        if path in excluded:
            continue
        code = line[:2]
        if code == "??":
            untracked.append(path)
            continue
        if code[0] not in (" ", "?"):
            staged.append(path)
        if code[1] not in (" ", "?"):
            unstaged.append(path)
    return WorkingTreeStatus(staged, unstaged, untracked)


def dirty_paths(root: Path, excludes: Sequence[str] = ()) -> set[str]:
    """Return the set of normalized paths with any uncommitted change (including untracked).

    Used to snapshot the main worktree before and after an isolated session so the caller can
    diff the two sets and find paths a session wrote outside its isolated worktree.
    """
    out = _git(root, "status", "--porcelain")
    return {_status_path(line) for line in _meaningful_status_lines(out, excludes)}


def _run_git_stdin(root: Path, args: Sequence[str],
                   input_text: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args], cwd=root, input=input_text,
            capture_output=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise AssentError("git executable not found; confirm git is installed and on PATH") from e


@dataclass(frozen=True)
class _EscapeClassification:
    """Escaped paths, split by how they can be ported back."""

    tracked: list[str]     # existing tracked files modified in place: ported via a patch
    untracked: list[str]   # brand-new files: ported via a content copy
    unsupported: list[str]  # deletes/renames/type changes/anything else: not auto-portable


def _classify_escape_paths(root: Path, paths: Sequence[str]) -> _EscapeClassification:
    codes: dict[str, str] = {}
    for line in _git(root, "status", "--porcelain").splitlines():
        if not line.strip():
            continue
        codes[_status_path(line)] = line[:2]

    tracked: list[str] = []
    untracked: list[str] = []
    unsupported: list[str] = []
    for path in paths:
        code = codes.get(path)
        if code == "??":
            untracked.append(path)
        elif code in (" M", "M ", "MM"):
            tracked.append(path)
        else:
            # Missing (no longer dirty), deleted, renamed, type-changed, or a conflict: none
            # of these has a safe, unambiguous automatic port-back, so refuse to guess.
            unsupported.append(path)
    return _EscapeClassification(tracked, untracked, unsupported)


def port_back_main_tree_escape(main_root: Path, worktree_root: Path,
                               paths: Sequence[str]) -> tuple[bool, str | None]:
    """Port paths a session wrote into ``main_root`` back into ``worktree_root``, then restore
    ``main_root`` at those paths -- all or nothing.

    Every path is validated (patch dry-run / target-collision check) before anything is
    mutated. If every check passes, the worktree is updated and only then is the main tree
    restored, so a mid-way failure cannot leave the main tree reverted while the escaped
    content is lost -- token-burned output is never discarded. Returns ``(True, None)`` on
    success, or ``(False, reason)`` with *both* trees left completely untouched.
    """
    plan = _classify_escape_paths(main_root, paths)
    if plan.unsupported:
        shown = ", ".join(sorted(plan.unsupported)[:5])
        more = " ..." if len(plan.unsupported) > 5 else ""
        return False, f"unsupported change type for automatic port-back: {shown}{more}"

    diffs: dict[str, str] = {path: _git(main_root, "diff", "--", path)
                             for path in plan.tracked}
    for path, diff_text in diffs.items():
        if not diff_text.strip() or _run_git_stdin(
                worktree_root, ["apply", "--check", "-"], diff_text).returncode != 0:
            return False, f"the worktree copy of {path} has diverged from the main tree"
    for path in plan.untracked:
        if (worktree_root / path).exists():
            return False, f"the worktree already has a conflicting {path}"

    applied_tracked: list[str] = []
    applied_untracked: list[str] = []
    try:
        for path, diff_text in diffs.items():
            result = _run_git_stdin(worktree_root, ["apply", "-"], diff_text)
            if result.returncode != 0:
                raise AssentError(
                    f"git apply failed for {path}: "
                    f"{result.stderr.strip() or result.stdout.strip()}")
            applied_tracked.append(path)
        for path in plan.untracked:
            dst = worktree_root / path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(main_root / path, dst)
            applied_untracked.append(path)
    except (AssentError, OSError) as e:
        for path in applied_untracked:
            (worktree_root / path).unlink(missing_ok=True)
        for path in reversed(applied_tracked):
            _run_git_stdin(worktree_root, ["apply", "-R", "-"], diffs[path])
        return False, f"porting escaped changes into the worktree failed: {e}"

    for path in plan.tracked:
        _git(main_root, "checkout", "--", path)
    for path in plan.untracked:
        (main_root / path).unlink(missing_ok=True)
    return True, None


def folder_worktree(root: Path, folder: str) -> Path | None:
    """Return a folder's valid fixed worktree, if it exists."""
    primary = main_worktree(root)
    path = worktree_path(primary, folder)
    if is_repo_worktree(primary, path):
        return path.resolve()
    return None


def folder_branches(root: Path, folder: str) -> list[str]:
    """Return local branches belonging to ``<folder>/*``."""
    return branches_with_prefix(root, f"{folder}/")


def unique_folder_branch(root: Path, folder: str) -> str | None:
    """Return the folder's sole local branch, or refuse an ambiguous set."""
    branches = folder_branches(root, folder)
    if not branches:
        return None
    if len(branches) != 1:
        raise AssentError(
            f"task folder {folder} has multiple local branches: {', '.join(branches)}")
    return branches[0]


def branch_tip(root: Path, branch: str) -> str:
    """Return the full commit hash at a branch tip."""
    return commit_of(root, branch)


@dataclass(frozen=True)
class FolderSourceSnapshot:
    """Immutable identity of a folder's sole clean, attached source."""

    folder: str
    branch: str
    worktree: Path
    tip: str


def resolve_folder_source(
        root: Path, folder: str,
        excludes: Sequence[str] = ()) -> FolderSourceSnapshot:
    """Resolve a folder's current source without guessing from historical metadata.

    Speculative stacking needs stronger evidence than ordinary cleanup discovery:
    the fixed worktree must exist, be clean and attached to the folder's one and
    only local branch.  Reading the tip twice detects a branch that moves during
    resolution instead of returning a mixed snapshot.
    """
    primary = main_worktree(Path(root).resolve())
    branches = folder_branches(primary, folder)
    if not branches:
        raise AssentError(
            f"upstream folder {folder} has no {folder}/* source branch")
    if len(branches) != 1:
        raise AssentError(
            f"upstream folder {folder} has ambiguous source branches: "
            f"{', '.join(branches)}")
    branch = branches[0]

    worktree = folder_worktree(primary, folder)
    if worktree is None:
        raise AssentError(
            f"upstream folder {folder} has no valid fixed source worktree")
    attached = current_branch(worktree)
    if not attached:
        raise AssentError(
            f"upstream folder {folder} source worktree {worktree} is detached")
    if attached != branch:
        raise AssentError(
            f"upstream folder {folder} source worktree {worktree} is on foreign "
            f"branch {attached}; expected {branch}")
    if not working_tree_status(worktree, excludes).is_clean:
        raise AssentError(
            f"upstream folder {folder} source worktree {worktree} is dirty")

    tip = branch_tip(primary, branch)
    worktree_tip = commit_of(worktree, "HEAD")
    confirmed_tip = branch_tip(primary, branch)
    if worktree_tip != tip or confirmed_tip != tip:
        raise AssentError(
            f"upstream folder {folder} source changed while its tip was being resolved")
    return FolderSourceSnapshot(folder, branch, worktree, tip)


# Machine-readable evidence recorded on an accept merge.
ACCEPT_TRAILER_FOLDER = "Assent-Folder"
ACCEPT_TRAILER_SOURCE_BRANCH = "Assent-Source-Branch"
ACCEPT_TRAILER_SOURCE_TIP = "Assent-Source-Tip"
ACCEPT_TRAILER_VERIFIED_TREE = "Assent-Verified-Tree"
ACCEPT_TRAILER_VERIFIER_SHA256 = "Assent-Verifier-SHA256"

_OBJECT_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_evidence_value(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise AssentError(f"accept evidence {name} must be a non-empty string")
    if value != value.strip():
        raise AssentError(
            f"accept evidence {name} must not have leading or trailing whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7f
           for character in value):
        raise AssentError(
            f"accept evidence {name} must not contain control characters")


def build_accept_trailers(folder: str, source_branch: str,
                          source_tip: str, verified_tree: str,
                          verifier_sha256: str) -> str:
    """Build passive, human-readable audit metadata for an accept merge."""
    _validate_evidence_value("folder", folder)
    _validate_evidence_value("source branch", source_branch)
    _validate_evidence_value("source tip", source_tip)
    _validate_evidence_value("verified tree", verified_tree)
    _validate_evidence_value("verifier digest", verifier_sha256)
    if not source_branch.startswith(f"{folder}/") or source_branch == f"{folder}/":
        raise AssentError(
            f"accept evidence source branch must belong to task folder {folder}")
    if not _OBJECT_ID_RE.fullmatch(source_tip):
        raise AssentError(
            "accept evidence source tip must be a 40- or 64-character lowercase "
            "hexadecimal object id")
    if not _OBJECT_ID_RE.fullmatch(verified_tree):
        raise AssentError(
            "accept evidence verified tree must be a 40- or 64-character lowercase "
            "hexadecimal object id")
    if not _SHA256_RE.fullmatch(verifier_sha256):
        raise AssentError(
            "accept evidence verifier digest must be a 64-character lowercase "
            "hexadecimal SHA-256 digest")
    return (f"{ACCEPT_TRAILER_FOLDER}: {folder}\n"
            f"{ACCEPT_TRAILER_SOURCE_BRANCH}: {source_branch}\n"
            f"{ACCEPT_TRAILER_SOURCE_TIP}: {source_tip}\n"
            f"{ACCEPT_TRAILER_VERIFIED_TREE}: {verified_tree}\n"
            f"{ACCEPT_TRAILER_VERIFIER_SHA256}: {verifier_sha256}")


def accept_commit_message(subject: str, folder: str, source_branch: str,
                          source_tip: str, verified_tree: str,
                          verifier_sha256: str) -> str:
    """Compose a one-line subject and passive accept audit metadata."""
    _validate_evidence_value("subject", subject)
    trailers = build_accept_trailers(
        folder, source_branch, source_tip, verified_tree, verifier_sha256)
    return f"{subject}\n\n{trailers}\n"


def _temporary_container(root: Path) -> Path:
    return root.parent / f"{root.name}.integration"


def _cleanup_temporary_worktree(root: Path, path: Path,
                                branch: str | None = None) -> None:
    """Remove this call's temporary resources and report any incomplete cleanup."""
    problems: list[str] = []
    if path.exists():
        removed = _run_git(root, "worktree", "remove", "--force", str(path))
        if removed.returncode != 0:
            try:
                shutil.rmtree(path)
            except OSError as e:
                problems.append(f"unable to remove temporary path {path}: {e}")
    pruned = _run_git(root, "worktree", "prune")
    if pruned.returncode != 0:
        problems.append(
            "git worktree prune failed: "
            f"{pruned.stderr.strip() or pruned.stdout.strip() or 'unknown error'}")
    if branch is not None:
        exists = _run_git(root, "show-ref", "--verify", "--quiet",
                          f"refs/heads/{branch}")
        if exists.returncode == 0:
            deleted = _run_git(root, "branch", "-D", branch)
            if deleted.returncode != 0:
                problems.append(
                    f"unable to delete temporary branch {branch}: "
                    f"{deleted.stderr.strip() or deleted.stdout.strip() or 'unknown error'}")
        elif exists.returncode not in (1,):
            problems.append(f"unable to inspect temporary branch {branch}")
    if path.exists():
        problems.append(f"temporary worktree path remains: {path}")
    with contextlib.suppress(OSError):
        _temporary_container(root).rmdir()
    if problems:
        raise AssentError("Temporary integration cleanup was incomplete: "
                          + "; ".join(problems))


def _commit_snapshot(root: Path, committish: str) -> str:
    _validate_evidence_value("commit", committish)
    return commit_of(root, f"{committish}^{{commit}}")


@dataclass(frozen=True)
class MergeOutcome:
    """Result of a no-fast-forward merge attempt."""

    ok: bool
    conflicts: tuple[str, ...] = ()
    exit_code: int = 0


def conflict_paths(worktree: Path) -> list[str]:
    """List unmerged paths in stable order."""
    out = _git(worktree, "diff", "--name-only", "--diff-filter=U")
    return sorted(line.strip() for line in out.splitlines() if line.strip())


def merge_no_ff(worktree: Path, commit: str, message: str) -> MergeOutcome:
    """Merge an explicit commit without fast-forwarding or resolving conflicts."""
    snapshot = _commit_snapshot(worktree, commit)
    result = _run_git(worktree, "merge", "--no-ff", "-m", message, snapshot)
    if result.returncode == 0:
        return MergeOutcome(True)
    conflicts = conflict_paths(worktree)
    if not conflicts:
        raise AssentError(
            f"git merge --no-ff {snapshot} failed (exit code {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}")
    _run_git(worktree, "merge", "--abort")
    return MergeOutcome(False, tuple(conflicts), result.returncode)


def tree_of(root: Path, committish: str) -> str:
    """Return the full tree object id for a commit or tree-ish."""
    _validate_evidence_value("tree-ish", committish)
    return _git(root, "rev-parse", f"{committish}^{{tree}}").strip()


def commit_parents(root: Path, committish: str = "HEAD") -> tuple[str, ...]:
    """Return one commit's parent object ids without scanning repository history."""
    snapshot = _commit_snapshot(root, committish)
    fields = _git(root, "show", "-s", "--format=%P", snapshot).strip().split()
    if not all(_OBJECT_ID_RE.fullmatch(parent) for parent in fields):
        raise AssentError("unable to parse Git commit parents")
    return tuple(fields)


def object_type(root: Path, object_id: str) -> str:
    """Return an object's Git type, failing if the object does not exist."""
    _validate_evidence_value("object id", object_id)
    return _git(root, "cat-file", "-t", object_id).strip()


def fast_forward(root: Path, commit: str) -> None:
    """Advance the checked-out branch only when Git can fast-forward it."""
    snapshot = _commit_snapshot(root, commit)
    _git(root, "merge", "--ff-only", snapshot)


# --- Conflict reconciliation primitives ---


def reconcile_worktree_path(root: Path, folder: str) -> Path:
    """Return the fixed reconciliation worktree path for one task folder.

    A sibling container beside ``<repo>.worktrees`` keeps a reconciliation out of
    the folder-source namespace, so a reconciliation worktree can never be
    mistaken for (or clean up as) a folder's own source worktree.
    """
    return root.parent / f"{root.name}.reconcile" / folder


def branch_exists(root: Path, branch: str) -> bool:
    """Report whether a local branch ref exists; a broken query fails loudly."""
    result = _run_git(root, "show-ref", "--verify", "--quiet",
                      f"refs/heads/{branch}")
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise AssentError(
        f"unable to inspect branch {branch} (exit code {result.returncode}): "
        f"{result.stderr.strip() or result.stdout.strip()}")


def add_worktree_branch(root: Path, branch: str, path: Path,
                        start_commit: str) -> None:
    """Create a worktree on a new branch starting at an exact commit."""
    snapshot = _commit_snapshot(root, start_commit)
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", branch, str(path), snapshot)


def merge_no_commit(worktree: Path, commit: str) -> MergeOutcome:
    """Merge an explicit commit without fast-forwarding, committing, or auto-aborting.

    Unlike :func:`merge_no_ff`, a textual conflict is deliberately left in the
    worktree's merge state so a human can resolve it there; the caller owns every
    later decision about that state.
    """
    snapshot = _commit_snapshot(worktree, commit)
    result = _run_git(worktree, "merge", "--no-ff", "--no-commit", snapshot)
    if result.returncode == 0:
        return MergeOutcome(True)
    conflicts = conflict_paths(worktree)
    if not conflicts:
        raise AssentError(
            f"git merge --no-ff --no-commit {snapshot} failed "
            f"(exit code {result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}")
    return MergeOutcome(False, tuple(conflicts), result.returncode)


def merge_head(worktree: Path) -> str | None:
    """Return ``MERGE_HEAD`` while a merge is in progress, otherwise ``None``."""
    result = _run_git(worktree, "rev-parse", "--quiet", "--verify",
                      "MERGE_HEAD^{commit}")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def abort_merge(worktree: Path) -> None:
    """Abort an in-progress merge, returning to the state before it started."""
    _git(worktree, "merge", "--abort")


def stage_paths(worktree: Path, paths: Sequence[str]) -> None:
    """Stage exactly the named paths, including a path the human deleted."""
    if not paths:
        return
    _git(worktree, "add", "-A", "--", *[_normalize(path) for path in paths])


def diff_cached_check(worktree: Path) -> str | None:
    """Return git's complaint about the staged content, or ``None`` when it is clean.

    ``git diff --cached --check`` is what catches a leftover conflict marker, so a
    "resolution" that still contains one never reaches a commit.
    """
    result = _run_git(worktree, "diff", "--cached", "--check")
    if result.returncode == 0:
        return None
    return (result.stdout.strip() or result.stderr.strip()
            or f"git diff --cached --check exited {result.returncode}")


def commit_merge(worktree: Path, message: str) -> str:
    """Commit the staged in-progress merge and return the new commit hash."""
    if merge_head(worktree) is None:
        raise AssentError(f"no merge is in progress in {worktree}")
    _git(worktree, "commit", "-m", message)
    return commit_of(worktree, "HEAD")


@contextlib.contextmanager
def temporary_integration_worktree(
        root: Path, folder: str,
        target_snapshot: str) -> Iterator[tuple[Path, str]]:
    """Create a temporary branch/worktree from an explicit target snapshot."""
    _validate_evidence_value("folder", folder)
    primary = main_worktree(root)
    snapshot = _commit_snapshot(primary, target_snapshot)
    suffix = uuid.uuid4().hex
    branch = f"assent-integration/{folder}/{suffix}"
    path = _temporary_container(primary) / f"target-{suffix}"
    path.parent.mkdir(parents=True, exist_ok=True)
    primary_error: BaseException | None = None
    try:
        _git(primary, "worktree", "add", "-b", branch, str(path), snapshot)
        yield path, branch
    except BaseException as e:
        primary_error = e
        raise
    finally:
        try:
            _cleanup_temporary_worktree(primary, path, branch)
        except AssentError as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(str(cleanup_error))
