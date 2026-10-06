"""A user's language and formats, kept with the account so they follow the user to every
device. Each field left empty takes what the browser asks for, or the language's own."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints


class Preferences(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    language: Literal["en", "sv", "de"] | None = None
    region: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] | None = None
    """A country, by its ISO 3166 code: with the language, it gives the formats."""
    dates: Literal["iso"] | None = None
    """ISO 8601 dates (2026-10-06) whatever the region; empty: the region's."""
    clock: Literal["24", "12"] | None = None
    decimal: Literal["point", "comma"] | None = None
