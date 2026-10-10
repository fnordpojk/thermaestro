"""Names for Home Assistant: the same ones the pages show, in the language the discovery
setting asks for, whoever is logged in."""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from . import i18n, labels
from .i18n import mark

if TYPE_CHECKING:
    from .operations import Services

TEXTS = {
    "price": mark("Electricity price"),
    "plugin": mark("Plugin %(name)s"),
    "attention": mark("Needs attention"),
    "warmer": mark("%(part)s: warmer, please"),
    "cooler": mark("%(part)s: cooler, please"),
    "boost": mark("%(part)s: one extra charge"),
    "fireplace": mark("A fireplace is on"),
    "intents": mark("Intents in force"),
    "deadline": mark("Next hot water"),
    "mode": mark("%(lever)s: mode"),
    "last_change": mark("Last change"),
}
"""Thermaestro's own entities, by the name the publisher asks for."""


@contextmanager
def _speaking(language: str) -> Iterator[None]:
    with i18n.using(language, i18n.Formats(i18n.format_locale(language, None))):
        yield


class Names:
    def __init__(self, services: "Services") -> None:
        self._services = services

    def point(self, instance: str, path: str, language: str) -> str:
        with _speaking(language):
            return self._services.label(instance, path)

    def node(self, instance: str, path: str) -> str | None:
        hub = self._services.sensors
        return hub.names.get(f"{instance}:{path}") if hub is not None else None

    def category(self, instance: str, path: str, given: str | None) -> str | None:
        return self._services.shown_as(instance, path, given)

    def text(self, what: str, language: str, **values: object) -> str:
        with _speaking(language):
            return i18n._(TEXTS[what], **values)

    def part(self, instance: str, path: str, language: str) -> str:
        own = self.node(instance, path)
        if own:
            return own
        host = self._services.host
        found = host.instances.get(instance) if host is not None else None
        nodes = {n.path: n for n in found.described.nodes} if found and found.described else {}
        node = nodes.get(path)
        with _speaking(language):
            return (
                labels.node(node.kind if node else None, path, node.label if node else None) or path
            )

    def lever(self, path: str, language: str) -> str:
        with _speaking(language):
            return labels.lever(path)
