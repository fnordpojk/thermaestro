"""The one-time code that lets the first administrator be created in the web UI.

Until an administrator exists, every start writes a new code to a file in the state
directory that only the service user can read, and logs where it is. Whoever creates the
first administrator must enter it, which proves they can read files on the host. It
lasts 60 minutes and works once; `thermaestro setup-code` prints it, or a new one.
"""

import contextlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from pathlib import Path

from ..files import UnsafePath, check_private_file, write_private

LIFETIME_S = 3600.0
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
"""No 0/O, 1/I/L or U, which are easy to mistake for others."""


def _normal(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


class SetupCode:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock

    def issue(self) -> str:
        """A new code, replacing any earlier one."""
        raw = "".join(secrets.choice(ALPHABET) for _ in range(12))
        code = f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"
        body = {"code": code, "expires": self._clock() + LIFETIME_S}
        write_private(self.path, json.dumps(body).encode())
        return code

    def current(self) -> str | None:
        """The code, if there is one that is still valid."""
        try:
            check_private_file(self.path)
            body = json.loads(self.path.read_text())
            code, expires = str(body["code"]), float(body["expires"])
        except (FileNotFoundError, UnsafePath, ValueError, KeyError, TypeError):
            return None
        return code if self._clock() < expires else None

    def redeem(self, entered: str) -> bool:
        """Whether `entered` is the valid code; if it is, it can't be used again."""
        code = self.current()
        if code is None or not hmac.compare_digest(
            _normal(code).encode(), _normal(entered).encode()
        ):
            return False
        self.discard()
        return True

    def discard(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
