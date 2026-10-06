"""The secrets file: tokens, passwords and keys, by name.

One file, separate from the settings, readable by the service user only and written
whole and atomically, so a crash leaves the old file or the new one. Backups and
exports of the settings can leave it out. Values never appear in a repr, a log line or
an error message.

A rotated token (an OAuth refresh token that may work only once) must be stored before
the old one is given up: `replace` returns only once the new one is on disk.
"""

import asyncio
import json
import logging
from pathlib import Path

from pydantic import SecretStr, TypeAdapter, ValidationError

from ..files import UnsafePath, check_private_file, write_private
from .errors import StoreError
from .settings import SecretName

log = logging.getLogger(__name__)

FORMAT = 1


class SecretStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()
        self._values: dict[str, str] | None = None

    def __repr__(self) -> str:
        return f"SecretStore({str(self.path)!r})"

    async def names(self) -> list[str]:
        async with self._lock:
            return sorted(await self._loaded())

    async def get(self, name: SecretName) -> SecretStr | None:
        async with self._lock:
            value = (await self._loaded()).get(name)
        return None if value is None else SecretStr(value)

    async def set(self, name: SecretName, value: str | SecretStr) -> None:
        """Store `value`; it is on disk when this returns."""
        plain = value.get_secret_value() if isinstance(value, SecretStr) else value
        async with self._lock:
            values = dict(await self._loaded())
            values[_checked(name)] = plain
            await self._write(values)
        log.info("secret %s stored", name)

    async def replace(self, name: SecretName, new: str | SecretStr, *, old: SecretStr) -> bool:
        """Replace `old` with `new`, unless something else already replaced it. True if
        this did; the old value may be given up only then."""
        plain = new.get_secret_value() if isinstance(new, SecretStr) else new
        async with self._lock:
            values = dict(await self._loaded())
            if values.get(_checked(name)) != old.get_secret_value():
                return False
            values[name] = plain
            await self._write(values)
        log.info("secret %s rotated", name)
        return True

    async def delete(self, name: SecretName) -> bool:
        async with self._lock:
            values = dict(await self._loaded())
            if values.pop(name, None) is None:
                return False
            await self._write(values)
        log.info("secret %s deleted", name)
        return True

    async def _loaded(self) -> dict[str, str]:
        if self._values is None:
            self._values = await asyncio.to_thread(self._read)
        return self._values

    def _read(self) -> dict[str, str]:
        try:
            check_private_file(self.path)
            data = json.loads(self.path.read_bytes())
        except FileNotFoundError:
            return {}
        except UnsafePath as e:
            raise StoreError(str(e)) from None
        except (OSError, ValueError) as e:
            # The message says where, never what: a parse error can quote the file.
            raise StoreError(f"{self.path} can't be read ({type(e).__name__})") from None
        secrets = data.get("secrets") if isinstance(data, dict) else None
        if data.get("format") != FORMAT or not isinstance(secrets, dict):
            raise StoreError(f"{self.path} isn't a format {FORMAT} secrets file")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in secrets.items()):
            raise StoreError(f"{self.path}: every secret is a name and a string")
        return secrets

    async def _write(self, values: dict[str, str]) -> None:
        data = json.dumps({"format": FORMAT, "secrets": values}, indent=1, sort_keys=True)
        await asyncio.to_thread(write_private, self.path, data.encode() + b"\n")
        self._values = values


_NAME: TypeAdapter[str] = TypeAdapter(SecretName)


def _checked(name: str) -> str:
    try:
        return _NAME.validate_python(name)
    except ValidationError:
        raise StoreError(f"{name!r} isn't a secret's name") from None
