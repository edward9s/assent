# Translating this project's documentation

English is the canonical language for this project's reader documentation;
Traditional Chinese (Taiwan usage, `zh-TW`) is maintained as a complete,
best-effort translation. This document defines how the translation is kept
in sync and how a contributor should work with it.

## Canonical and translation paths

| Canonical (English) | Translation (Traditional Chinese) |
|---|---|
| `README.md` | `README.zh-TW.md` |
| `docs/CONSENSUS.md` | `docs/zh-TW/CONSENSUS.md` |
| `docs/TRANSLATING.md` | not translated (contributor-facing process doc) |

New long-form reader documents follow the same pattern: the canonical file
sits next to its topic, and its translation sits at
`docs/zh-TW/<same file name>`, except for root-level files such as
`README.md`, whose translation uses the `README.<locale>.md` sibling form.

`zh-TW` is written exactly as `zh-TW` (capital TW, hyphen, no underscore),
matching the BCP 47 tag for Traditional Chinese as used in Taiwan. Do not
use `zh-Hant`, `zh_TW`, `zh-tw`, or `cn`.

## Taiwan terminology

Translated prose uses Traditional Chinese characters and Taiwan usage, not
mainland-China phrasing transliterated word-for-word. For example, when
naming the writing system itself, prefer the Taiwan-usage term ("orthodox
script") over the mainland-usage term ("complex/traditional script"), and
avoid importing mainland-specific tech jargon (e.g. render a term like
"dogfooding" with a plain Taiwan-usage paraphrase rather than a literal
mainland-coined loan translation) when a clearer Taiwan-usage phrasing
exists. When in doubt, match the terminology already used in this
project's existing Traditional Chinese files.

## What stays untranslated

Identifiers, public APIs, CLI flags and subcommand names, TOML keys and
example values, file paths, error text, commit message formats, and code or
command literals inside fenced blocks are copied verbatim into the
translation, never translated as if they were prose. Section structure
(heading levels, table columns, code block order) should mirror the
canonical document so the two stay easy to diff side by side.

## Relative-link rules

Every relative link inside a translated document must resolve to an
existing path in the repository from that document's own location — the
translation lives at a different directory depth than the canonical file
(for example `docs/zh-TW/CONSENSUS.md` is one level deeper than
`docs/CONSENSUS.md`), so link targets are not copied as literal strings;
recompute each relative path from the translation's actual location. Links
between the canonical file and its translation must be reciprocal: the
canonical file links to the translation, and the translation links back to
the canonical file, near the top of both documents.

## Required translation header

Every translated file opens with a link back to its canonical source and a
one-line note stating that it is a translation, that English prevails if
the two diverge, and which canonical commit or version it was translated
from, for example:

```markdown
# <translated title>

*[English](<relative link to the canonical file>)*

> This file is a Traditional Chinese (Taiwan usage) translation of
> [<canonical file>](<relative link>). If content disagrees with the
> English version, the English version prevails. Translated from commit
> `<short hash>` (`<date>`).
```

A contributor updating the translation replaces the recorded commit hash
and date with the commit they reviewed against, so the next translator can
tell at a glance how far the translation has drifted.

## Keeping translations in sync

A pull request that changes README core content (installation, quick
start, command syntax, positional arguments, the model/effort abstraction,
worktree safety, task/log filename rules, or clean/reject/rework semantics)
must update `README.zh-TW.md` in the same PR — these are the sections a new
contributor reads first, and a stale copy there actively misleads.

Longer design-rationale translations (such as `docs/zh-TW/CONSENSUS.md`) may
lag the canonical document on a best-effort basis. A translation that has
fallen behind must say so rather than silently claiming parity: update the
translated-commit note in its header to the commit it was last checked
against, so a reader can see how far it has drifted instead of assuming it
is current.

## Scope

Traditional Chinese reader documentation is translated prose only. It never
duplicates an executable contract: `agents/templates/format.md` and the
rest of the packaged templates are English-only, and `agents init` never
generates a second-language variant of a task-format or session-rules file.
`.agents/instructions.md` is the one documented session-rules path in every
language; translated documentation refers to it by that same path rather
than introducing a second name or a migration story.
