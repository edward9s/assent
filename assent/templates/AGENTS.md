# Project instructions

## Project

<!-- TODO: one sentence describing the project: stack, platform, purpose -->

## Permanent constraints

<!-- TODO: hard constraints that always hold; decisions that stay valid across plans also settle here -->

- When using assent, first read `.assent/instructions.md` in the project's main worktree; a worktree session uses the absolute path the scheduler provides. <!-- assent-instructions -->

- Work-folder `after` dependencies control both scheduler readiness and the
  Git base. At most one unaccepted upstream may be stacked; multiple
  unaccepted upstreams fail closed. Preserve stale downstream work for an
  explicit rework/reject or new plan, and clean upstreams only after direct
  dependents are accepted and mechanically proven integrated.
