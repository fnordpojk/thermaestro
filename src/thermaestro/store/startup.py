"""The start-up file: what's needed before anything else, and nothing set in the UI.

Thermaestro reads it and never writes it, so on a Pi it stays a package configuration
file that upgrades keep. A missing file means every default.

    [web]
    listen = "0.0.0.0"
    port = 8080

    [paths]
    state = "/var/lib/thermaestro"
"""

import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import StoreError, name_fields
from .paths import Layout

Port = Annotated[int, Field(ge=1, le=65535)]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Web(_Model):
    listen: str = "0.0.0.0"  # noqa: S104  # the web UI is for the whole LAN
    port: Port = 8080


class Paths(_Model):
    state: Path | None = None
    """Instead of the state directory systemd or the container gives."""


class Startup(_Model):
    web: Web = Web()
    paths: Paths = Paths()

    def layout(self, layout: Layout) -> Layout:
        if self.paths.state is None:
            return layout
        return Layout(layout.config, self.paths.state)


def load_startup(path: Path) -> Startup:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Startup()
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise StoreError(f"{path}: {e}") from e
    try:
        return Startup.model_validate(data)
    except ValidationError as e:
        raise name_fields(str(path), e) from e
