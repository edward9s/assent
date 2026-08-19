"""Neutral ownership of one plan's current Git source and finished set.

Accept, clean, reject, and reconcile all answer the same two questions before
they touch anything: which single ``<plan>/*`` branch is the plan's current
source, and which task statuses count as finished.  Both answers were private
to ``assent.accept``, so every other command had to reach into the module that
publishes work.  They live here instead, giving the contract one neutral owner
that no command module depends on privately.

This module only resolves and reports; it never mutates a ref, a worktree, or a
task file, so acquiring locks and deciding what to do with the answer stays with
the calling command.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from assent import AssentError, gitops

# The task statuses that make a whole plan finished.  Written once here so
# accept, clean, reject, and reconcile cannot drift apart on what "finished"
# means.
COMPLETE_STATUSES = ("DONE", "SKIP")


def resolve_source_snapshot(main: Path, plan_name: str,
                            excludes: Sequence[str], *,
                            operation: str = "accept"
                            ) -> tuple[str, str, Path | None]:
    """Resolve the only current source branch and require a clean attachment."""
    worktree = gitops.plan_worktree(main, plan_name)
    if worktree is not None:
        branch = gitops.current_branch(worktree)
        if not branch:
            raise AssentError(
                f"source worktree {worktree} is in detached HEAD state, so there "
                f"is no current source branch for {operation}")
        if not branch.startswith(f"{plan_name}/") or branch == f"{plan_name}/":
            raise AssentError(
                f"source worktree {worktree} is on branch {branch}, which is not "
                f"a {plan_name}/* branch")
        if not gitops.working_tree_status(worktree, excludes).is_clean:
            raise AssentError(f"source worktree {worktree} is not clean")
    else:
        branches = gitops.plan_branches(main, plan_name)
        if len(branches) > 1:
            if operation != "accept":
                raise AssentError(
                    f"current source is ambiguous ({', '.join(branches)})")
            raise AssentError(
                "multiple candidate source branches exist "
                f"({', '.join(branches)}); accept does not guess which one is current")
        if not branches:
            if operation != "accept":
                raise AssentError(
                    f"no source worktree or {plan_name}/* branch exists")
            raise AssentError(
                f"no source worktree or {plan_name}/* branch exists; accept does not "
                "infer authorization from old commit messages")
        branch = branches[0]
    return branch, gitops.branch_tip(main, branch), worktree
