"""Git operations: worktree, branches, clean/scope checks, commit, restore.

All Git commands run with cwd=project root, and the return code is checked before use;
a missing git binary gets a clear error message instead of a traceback. `excludes` is a
list of relative paths for runtime artifacts (_agents.log, _report.md, etc.): they are
never input or checkpoint content, so they never take part in clean checks, scope checks,
or commits.
"""
from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

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


def ensure_worktree(root: Path, folder: str) -> Path:
    """Create or reuse a fixed-path, detached-HEAD git worktree."""
    root = root.resolve()
    path = worktree_path(root, folder)
    if path.exists():
        if path.is_dir() and _is_repo_worktree(root, path):
            return path
        raise AssentError(
            f"worktree path already exists but is not a valid worktree of this repo: {path}")

    # If the path was manually deleted, clear stale worktree metadata still held by the main repo.
    _git(root, "worktree", "prune")
    _git(root, "worktree", "add", "--detach", str(path))
    return path


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

    Entries already covered by .gitignore (e.g. a project where the whole .agents/ is
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
