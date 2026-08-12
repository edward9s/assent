# Translating reader documentation

English is canonical. Traditional Chinese (`zh-TW`) is a reader translation;
when the two disagree, follow English.

## File layout

- `README.md` maps to `README.zh-TW.md`.
- `docs/NAME.md` maps to `docs/zh-TW/NAME.md`.
- This contributor note is not translated.

Every translated page links to its English source near the top, and every
English page links back. Resolve relative links from the translated file's own
directory.

## What to translate

Translate reader-facing prose into natural Traditional Chinese used in Taiwan.
Preserve meaning and structure, but rewrite awkward English instead of copying
its sentence order. Prefer plain language over literal jargon.

Keep these items verbatim:

- identifiers, API names, CLI commands and flags;
- TOML keys and values;
- paths, error text, commit formats, and code examples.

Update the English and Chinese page together whenever stale instructions could
mislead a user. A translated page needs no commit hash or version banner; Git
already records that history.

## Boundary

Translations are human guides, not executable contracts. The English-only
files installed as `~/.assent/instructions.md`, `~/.assent/format.md`, and
`~/.assent/workflow.md` remain the single AI contract set and are never copied
under a translated name.
