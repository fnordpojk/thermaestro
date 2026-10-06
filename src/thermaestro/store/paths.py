"""Where Thermaestro keeps things.

The configuration directory holds the small start-up file, which Thermaestro only reads.
The state directory holds everything set in the UI or learned: the database, the secrets
file, user-converted register maps and the audit log. Moving an installation, between a
Pi and Docker for instance, is a copy of the state directory.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CONFIG_DIR = Path("/etc/thermaestro")
STATE_DIR = Path("/var/lib/thermaestro")


@dataclass(frozen=True, slots=True)
class Layout:
    config: Path
    state: Path

    @classmethod
    def from_environment(cls, env: Mapping[str, str] = os.environ) -> "Layout":
        """systemd's ConfigurationDirectory= and StateDirectory= set these variables, and
        the Docker image sets them to /config and /data. Several directories arrive
        separated by colons; the first is ours."""
        config = env.get("CONFIGURATION_DIRECTORY", "").split(":")[0]
        state = env.get("STATE_DIRECTORY", "").split(":")[0]
        return cls(Path(config) if config else CONFIG_DIR, Path(state) if state else STATE_DIR)

    @property
    def startup(self) -> Path:
        return self.config / "thermaestro.toml"

    @property
    def database(self) -> Path:
        return self.state / "thermaestro.db"

    @property
    def secrets(self) -> Path:
        return self.state / "secrets.json"

    @property
    def maps(self) -> Path:
        """Register maps users converted from their own exports."""
        return self.state / "maps"

    @property
    def audit(self) -> Path:
        return self.state / "audit"
