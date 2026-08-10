"""Typed ability and agent-role configuration data."""
from __future__ import annotations

from dataclasses import dataclass

from assent import AssentError


@dataclass(frozen=True)
class Ability:
    """One atomic capability definition."""

    prompt: str
    writes: bool
    gate: bool
    produces_verdict: bool = False


@dataclass(frozen=True)
class Agent:
    """A named role composed from ordered ability references."""

    ability: tuple[str, ...]
    model: str | None = None
    effort: str | None = None


@dataclass(frozen=True)
class ResolvedAgent:
    """A role with its ability references resolved and aggregate flags derived."""

    abilities: tuple[Ability, ...]
    model: str | None
    effort: str | None
    writes: bool
    gate: bool
    produces_verdict: bool


def resolve_agent(name: str, agents: dict[str, Agent],
                  abilities: dict[str, Ability]) -> ResolvedAgent:
    """Resolve one named role without inferring behavior from ability names."""
    try:
        agent = agents[name]
    except KeyError as e:
        raise AssentError(f"Unknown agent role: {name!r}") from e

    resolved = tuple(abilities[ability] for ability in agent.ability)
    return ResolvedAgent(
        abilities=resolved,
        model=agent.model,
        effort=agent.effort,
        writes=any(ability.writes for ability in resolved),
        gate=any(ability.gate for ability in resolved),
        produces_verdict=any(ability.produces_verdict for ability in resolved),
    )
