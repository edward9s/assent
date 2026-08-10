"""Typed ability and role configuration data."""
from __future__ import annotations

from dataclasses import dataclass

from assent import AssentError


@dataclass(frozen=True)
class Ability:
    """One atomic capability definition."""

    prompt: str
    writes: bool
    produces_verdict: bool = False


@dataclass(frozen=True)
class Role:
    """A named role composed from ordered ability references."""

    ability: tuple[str, ...]
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class ResolvedRole:
    """A role with its ability references resolved and aggregate flags derived."""

    abilities: tuple[Ability, ...]
    model: str | None
    effort: str | None
    writes: bool
    produces_verdict: bool


def resolve_role(name: str, roles: dict[str, Role],
                 abilities: dict[str, Ability]) -> ResolvedRole:
    """Resolve one named role without inferring behavior from ability names."""
    try:
        role = roles[name]
    except KeyError as e:
        raise AssentError(f"Unknown role: {name!r}") from e

    resolved = tuple(abilities[ability] for ability in role.ability)
    return ResolvedRole(
        abilities=resolved,
        model=role.model,
        effort=role.effort,
        writes=any(ability.writes for ability in resolved),
        produces_verdict=any(ability.produces_verdict for ability in resolved),
    )
