"""Where in-process plugins come from.

A plugin package names a factory under the entry-point group `thermaestro.plugins`; the
name is the plugin's, as plugin settings refer to it (`nibe`). A plugin with no factory
here runs out of process and connects to the plugin socket.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from pydantic import JsonValue

from ..cap import Plugin
from ..store import Database, PluginState, SecretStore

GROUP = "thermaestro.plugins"


class PluginStore:
    """What one plugin instance keeps across restarts: a JSON object of its own, in the
    database. The plugin decides what goes in it, and how often it's written."""

    def __init__(self, db: Database, instance: str) -> None:
        self._db = db
        self._instance = instance

    async def load(self) -> dict[str, Any]:
        found = await self._db.get(PluginState, self._instance)
        return dict(found.data) if found is not None else {}

    async def save(self, data: dict[str, Any]) -> None:
        await self._db.put(PluginState(data=data), self._instance)


@dataclass(frozen=True, slots=True)
class PluginContext:
    instance: str
    """The instance's id, which tells several instances of one plugin apart."""
    settings: Mapping[str, JsonValue]
    secrets: SecretStore
    """Read by name; a setting holds a secret's name, never its value."""
    state: PluginStore | None = None
    """What the instance keeps across restarts."""


Factory = Callable[[PluginContext], Plugin]


def discover() -> dict[str, Factory]:
    return {ep.name: ep.load() for ep in entry_points(group=GROUP)}
