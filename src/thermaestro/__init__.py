"""Thermaestro, a heat-pump controller. This release is a placeholder."""

from importlib import metadata


def version() -> str:
    """The installed release's version."""
    try:
        return metadata.version("thermaestro")
    except metadata.PackageNotFoundError:
        return "0.0.0"
