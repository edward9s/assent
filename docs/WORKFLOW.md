# Workflow

*[README](../README.md) · [Traditional Chinese](zh-TW/WORKFLOW.md)*

Assent has three human-facing stages: agree on a plan, run it unattended, and
review the finished result. The AI reads different material at each stage so it
does not carry the whole system manual into every task.

## 1. Planning meeting

Start in the primary worktree. The AI reads:

1. the project's `AGENTS.md`;
2. `~/.assent/instructions.md` for shared session rules; and
3. `~/.assent/format.md` for the plan-file format.

It reads `~/.assent/workflow.md` only when the meeting changes workflow roles or
needs exact scheduler behavior. Source and tests are inspected as needed to
find real ownership and write a complete scope.

First confirm the requirements with the AI; create no files during that
discussion. After explicit human agreement, ask the AI: “Turn the consensus
above into an Assent-format plan under `.assent/<PLAN>/`.” The plan consists of
`tNNN_name.e.toml` tasks. Each task should tell a fresh AI what to change, what
it may write, and how focused verification decides completion.

Finish with:

```text
assent check
```

The plan is ready only when this passes.

### Planning prompt

```text
Let's plan this change together. Read AGENTS.md,
~/.assent/instructions.md, and ~/.assent/format.md. Answer concisely and do
not use subagents. Inspect relevant source and tests, and report any source bug,
bad structure, or documentation/runtime mismatch. Do not overengineer. Confirm
the requirements with me first; create no files before I explicitly agree.
After I agree, turn the consensus above into an Assent-format plan under
.assent/<PLAN>/ and run assent check.
```

## 2. Unattended execution

`assent run` opens only the AI sessions required by the configured workflow.
An ordinary task session reads project rules, short shared instructions, and
its assigned `.e.toml`; it then inspects the relevant source itself. The
scheduler supplies each role's responsibility and current evidence. The AI does
not read raw `assent.toml` to guess its job.

The three workflow layers have separate responsibilities:

- **Task:** implement one task. A failing focused test or a worker's `BLOCKED`
  result can enter the task reviewer/fixer. A passing first test skips repair.
- **Plan:** after every task is `DONE` or `SKIP`, check the cumulative worktree
  against the whole plan, including interactions between tasks.
- **Integration:** rebuild the exact selected result and run complete
  verification. If construction conflicts, repair those conflicts and rebuild
  the same selection.

Mechanical checks are decision points, not AI opinions. A pass completes the
layer. A failure may use the next configured repair role and is then rechecked.
The configured arrays are finite; Assent never creates extra repair rounds.

Task `BLOCKED` evidence remains at the task layer and never spends plan review.
If a task reviewer finds one omitted scope path, a write-capable reviewer fixes
that path in the same session; the scheduler validates the edits and updates
the `.e.toml` task file. Role names are user-defined—permissions and verdict
behavior come from abilities, not names.

If a mechanical check still fails when the budget ends, work and evidence are
kept. An undecidable review becomes `REVIEW UNRESOLVED, HUMAN DECISION` so other
queued plans can continue. Infrastructure or safety failures still stop with a
nonzero result.

Interrupted sessions become resumable WIP checkpoints when ownership is clear.
A clean legacy `DONE` task is left as history; Assent does not retroactively
synthesize a terminal auto checkpoint.

## 3. Acceptance review

Start with:

```text
assent report <PLAN>
```

Then inspect `_report.md`, the task files and relevant journal entries, the
checkpoint diff, implementation, and focused/full verification evidence. If
the report says `TECHNICAL DEBT REVIEW REQUIRED`, read `_technical_debt.md` and
decide every listed item before accepting.

Use an independent AI when a second opinion is useful. It should read
`AGENTS.md`, the three installed contracts, and the evidence above, then inspect
only the relevant source. The review does not modify or accept anything until
the human chooses an action.

### Acceptance-review prompt

```text
Act as an independent acceptance reviewer. Answer concisely and do not use
subagents. Read AGENTS.md and the Assent contracts, then inspect this plan's
_report.md, relevant task and journal files, checkpoint diff, implementation,
and verification evidence. Report evidence-based bugs, unmet requirements,
missing tests, harmful complexity, and documentation/runtime mismatches first.
If technical debt is flagged, list every item and ask for a disposition. This
review is human-driven: do not accept, rework, or edit anything. Wait for the
human decision.
```

The human then chooses one explicit action:

- `assent accept <PLAN>` publishes receipt-backed work;
- `assent rework <PLAN> <TASK>` reopens an existing task while preserving code;
- `assent reject <PLAN>` is a confirmed destructive reset: it checkpoints dirty
  edits, records branch tips, removes the managed worktree and same-prefix
  branches, then resets started tasks to `TODO`.

No workflow step accepts a plan. Verification supplies evidence; acceptance is
the decision.

## Dependencies and stacked work

`after` controls readiness only. `base` alone says that a downstream worktree
starts from one unaccepted upstream commit. Without `base`, it starts from the
current integration target. If an upstream changes after downstream work was
built on it, preserve the downstream result and use rework, rejection, or a new
plan instead of rewriting history.

See [Commands](COMMANDS.md), [Verification](VERIFICATION.md), and
[Operations](OPERATIONS.md) for the decisions made around this lifecycle.
