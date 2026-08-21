"""Portable model tiers and the exact ``model/effort`` vendor selection grammar.

A tier names one complete invocation: the vendor model and the vendor effort it is
always used at.  Effort is not an independent portable axis, so there is no abstract
effort vocabulary and no translation table -- a tier's configured value already carries
the exact strings the vendor CLI receives.
"""
from __future__ import annotations

from assent import AssentError


MODEL_TIERS = frozenset({"prime", "core", "lite"})
VENDOR_DEFAULT_EFFORT = "<vendor-default>"
SELECTION_SEPARATOR = "/"


def literal_value(value: str | None) -> str | None:
    """Return the exact vendor selection, or ``None`` when this is a portable tier.

    There is no marker syntax: a selection is either one of the three tiers or it is
    the vendor's own ``model/effort`` string.  Only ``assent.toml`` accepts the second
    form -- a task file's tier vocabulary is closed, so a vendor id cannot be written
    there at all rather than merely being discouraged.
    """
    if value is None or value in MODEL_TIERS:
        return None
    return value


def has_literal(model: str | None) -> bool:
    """Return whether a selection bypasses the portable tier mapping."""
    return literal_value(model) is not None


def effort_identity(value: str | None) -> str:
    """Return a stable display/persistence identity for an omitted effort flag."""
    return value if value is not None else VENDOR_DEFAULT_EFFORT


def split_selection(value: str, owner: str) -> tuple[str, str | None]:
    """Split one ``model`` or ``model/effort`` selection into its vendor strings.

    The separator is the first ``/``: everything before it is the model and everything
    after it is the effort the CLI receives.  A selection with no separator deliberately
    passes no effort argument and inherits the vendor CLI's own default.

    A model name may therefore not contain ``/``.  That restriction is what keeps an
    omitted effort unambiguous -- without it ``"vendor/model"`` could not be told apart
    from a model plus an effort -- so a second separator is refused here, at config load,
    rather than silently truncating the model name at run time.
    """
    model, separator, effort = value.partition(SELECTION_SEPARATOR)
    if not model:
        raise AssentError(
            f"{owner} has selection {value!r} with an empty model name")
    if not separator:
        return model, None
    if not effort:
        raise AssentError(
            f"{owner} has selection {value!r} with an empty effort after "
            f"{SELECTION_SEPARATOR!r}; omit the separator to use the vendor default")
    if SELECTION_SEPARATOR in effort:
        raise AssentError(
            f"{owner} has selection {value!r}; a model name must not contain "
            f"{SELECTION_SEPARATOR!r}, which separates the model from its effort")
    return model, effort


def parse_tier(value: str, owner: str) -> str:
    """Validate one portable tier for a task file, where nothing else is allowed.

    A task file names difficulty, never a vendor release.  Keeping the vocabulary closed
    here means a typo is refused while the plan is being read, with the valid words in
    the message, instead of travelling to a CLI as an unknown model id; it also makes a
    vendor id structurally impossible in a plan artifact that outlives that release.
    """
    selected = value.strip().lower()
    if selected not in MODEL_TIERS:
        raise AssentError(
            f"{owner} has model = {value.strip()!r}, which is not a valid model tier "
            f"({' / '.join(sorted(MODEL_TIERS))}). A task file states difficulty only; "
            f"vendor model ids belong in [adapter.<name>.models]"
        )
    return selected


def parse_selection(value: str, owner: str) -> str:
    """Validate one portable tier or one exact vendor ``model/effort`` selection.

    Used for ``assent.toml`` roles and workflow entries, the one place a vendor string
    is legitimate: a step bound to a single adapter may need a model outside that
    adapter's three tiers.  Anything that is not a tier is read as that vendor string,
    so its grammar is checked now rather than at invocation time.
    """
    selected = value.strip()
    if selected in MODEL_TIERS:
        return selected
    split_selection(selected, owner)
    return selected


