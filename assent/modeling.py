"""Portable model/effort names and their explicit literal escape syntax."""
from __future__ import annotations

from assent import AssentError


MODEL_TIERS = frozenset({"prime", "core", "lite"})
EFFORT_LEVELS = frozenset({"heavy", "normal", "slight"})
VENDOR_DEFAULT_EFFORT = "<vendor-default>"


def literal_value(value: str | None) -> str | None:
    """Return the exact value inside one already-validated ``[...]`` choice."""
    if value is None or not (value.startswith("[") and value.endswith("]")):
        return None
    return value[1:-1]


def has_literal(model: str | None, effort: str | None) -> bool:
    """Return whether either selection bypasses its portable mapping."""
    return literal_value(model) is not None or literal_value(effort) is not None


def effort_identity(value: str | None) -> str:
    """Return a stable display/persistence identity for an omitted effort flag."""
    return value if value is not None else VENDOR_DEFAULT_EFFORT


def _parse_choice(value: str, allowed: frozenset[str], kind: str,
                  owner: str, *, lowercase_abstract: bool) -> str:
    selected = value.strip()
    opens = selected.startswith("[")
    closes = selected.endswith("]")
    if opens or closes:
        if not (opens and closes):
            raise AssentError(
                f"{owner} has malformed literal {kind} {selected!r}; "
                "use one non-empty value inside [...]"
            )
        literal = selected[1:-1]
        if (not literal or literal != literal.strip()
                or "[" in literal or "]" in literal):
            raise AssentError(
                f"{owner} has malformed literal {kind} {selected!r}; "
                "use one non-empty value inside [...]"
            )
        return selected
    abstract = selected.lower() if lowercase_abstract else selected
    if abstract not in allowed:
        choices = " / ".join(sorted(allowed))
        description = "model tier" if kind == "model" else "effort"
        raise AssentError(
            f"{owner} has {kind} = {selected!r}, which is not a valid "
            f"{description} "
            f"({choices}, or [{kind.upper()}] for a literal adapter value)"
        )
    return abstract


def parse_model(value: str, owner: str, *, lowercase_abstract: bool = False) -> str:
    """Validate one portable tier or exact ``[MODEL]`` adapter value."""
    return _parse_choice(
        value, MODEL_TIERS, "model", owner,
        lowercase_abstract=lowercase_abstract)


def parse_effort(value: str, owner: str, *, lowercase_abstract: bool = False) -> str:
    """Validate one portable effort or exact ``[EFFORT]`` adapter value."""
    return _parse_choice(
        value, EFFORT_LEVELS, "effort", owner,
        lowercase_abstract=lowercase_abstract)


def inherited_effort(model_override: str | None,
                     effort_override: str | None,
                     fallback: str | None) -> str | None:
    """Choose a stated effort; a literal model with no effort uses vendor default."""
    if effort_override is not None:
        return effort_override
    if literal_value(model_override) is not None:
        return None
    return fallback
