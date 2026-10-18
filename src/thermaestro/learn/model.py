"""What Thermaestro has learned, as kept so a restart keeps it.

Two kinds:
- **observed**: a figure taken as seen, such as how long the tank's charges take. It is
  used from the first observation on, as the rules always have;
- **model**: a learned model's value, which drives no decision until it has beaten a
  simple baseline on data it wasn't fitted on (`proved`). Until then the starting value
  (a setup answer or a seed) stands.

The household can start a value over, or pin a number it knows better. A pinned number is
used instead of what was learned, and learning goes on beside it.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

State = Literal["starting", "learning", "proved", "frozen", "rejected"]
"""- starting: nothing learned yet; the starting value stands.
- learning: learned from too little, or not yet proved.
- proved: beat its baseline on held-out data, so it is used.
- frozen: can't be learned now (no heating in summer); kept as it was.
- rejected: lost to its baseline, or its data said nothing; the starting value stands."""

Kind = Literal["observed", "model"]

Number = float | dict[str, float]
"""A value: one number, or a few named ones (when a pump's periodic increase was seen)."""


class Learned(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """What it is: `tank.charge_s`."""
    scope: str
    """Where: `nibe:hp1/dhw`."""
    kind: Kind
    value: Number | None = None
    unit: str | None = None
    state: State = "starting"
    why: str | None = None
    """Why it is in its state, as a message for the web UI's catalog."""
    args: dict[str, float | int | str] = {}
    """The values `why` names, as `%(name)s`."""
    n: int = 0
    """How many observations or intervals it rests on."""
    since: float | None = None
    """When the first of them was, in seconds since the epoch."""
    updated: float | None = None
    starting: float | None = None
    """The setup answer or seed it replaces, where there is one."""
    pinned: float | None = None
    """A number the household set instead."""
    fingerprint: str | None = None
    """What it was learned from (sensors, answers): a change there starts it over."""

    @property
    def key(self) -> str:
        return key(self.name, self.scope)

    @property
    def used(self) -> Number | None:
        """What decisions use: the pinned number; else, for an observed value, what was
        seen; for a model, its value once proved (and while frozen, which only a proved one
        is), else its starting value."""
        if self.pinned is not None:
            return self.pinned
        if self.kind == "observed" or self.state in ("proved", "frozen"):
            return self.value if self.value is not None else self.starting
        return self.starting


def key(name: str, scope: str) -> str:
    return f"{name}@{scope}"


def mark(text: str) -> str:
    """Marks a message for the web UI's catalog: shown translated, kept in English here."""
    return text
