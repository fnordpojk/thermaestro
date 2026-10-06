"""What the core assumes about a lever, given what is known.

Unknown is treated conservatively, field by field. Any knowledge may make the core more
careful, but only documented, verified or user knowledge may make it less careful: a
reported "this lever works" doesn't let the planner use it unattended.
"""

from dataclasses import dataclass

from .model import Lever, Persistence, Range, Wear

STORED = Persistence(kind="stored")
FLASH = Wear(kind="flash")


@dataclass(frozen=True, slots=True)
class Assumed:
    works: bool
    """False: the lever is offered to the user as untested, never used unattended."""
    persistence: Persistence
    wear: Wear
    can_disable: dict[str, bool]
    """Per competing feature. False: the lever isn't taken over while it may be active."""
    ranges: dict[str, Range | None]
    """Per parameter. None: only values already observed on this device."""
    effect_delay_s: float | None
    """None: long, so follow-up waits before judging."""


def assume(lever: Lever) -> Assumed:
    return Assumed(
        works=lever.works.trusted and lever.works.value is True,
        persistence=_persistence(lever),
        wear=_wear(lever),
        can_disable={
            f.name: f.can_disable.trusted and f.can_disable.value is True
            for f in lever.competing_features
        },
        ranges={
            name: p.range.value if p.range.trusted else None for name, p in lever.params.items()
        },
        effect_delay_s=lever.effect_delay_s.value if lever.effect_delay_s.trusted else None,
    )


def _persistence(lever: Lever) -> Persistence:
    # Any stated persistence is careful in its own way: stored costs wear, and volatile or
    # leased means the setting must be applied again. Only a refuted one isn't used.
    k = lever.persistence
    if k.value is None or k.known == "refuted":
        return STORED
    return k.value


def _wear(lever: Lever) -> Wear:
    k = lever.wear
    if k.value is None or k.known == "refuted":
        return FLASH
    if k.trusted or k.value.kind != "none":
        return k.value
    return FLASH  # "no wear" is less careful, so it needs trusted knowledge
