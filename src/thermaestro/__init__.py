"""Thermaestro: plans heating and hot water around prices, weather and the household's goals."""

from importlib import metadata


def version() -> str:
    """The installed release's version."""
    try:
        return metadata.version("thermaestro")
    except metadata.PackageNotFoundError:
        return "0.0.0"
