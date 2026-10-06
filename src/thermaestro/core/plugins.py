"""Where in-process plugins come from.

A plugin package names a factory under the entry-point group `thermaestro.plugins`; the
name is the plugin's, as plugin settings refer to it (`nibe`). A plugin with no factory
here runs out of process and connects to the plugin socket.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points

from pydantic import JsonValue

from ..cap import Plugin
from ..store import SecretStore

GROUP = "thermaestro.plugins"


@dataclass(frozen=True, slots=True)
class PluginContext:
    instance: str
    """The instance's id, which tells several instances of one plugin apart."""
    settings: Mapping[str, JsonValue]
    secrets: SecretStore
    """Read by name; a setting holds a secret's name, never its value."""


Factory = Callable[[PluginContext], Plugin]


def discover() -> dict[str, Factory]:
    return {ep.name: ep.load() for ep in entry_points(group=GROUP)}
