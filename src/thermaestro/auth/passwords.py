"""Passwords: the rules for choosing one, and hashing.

The rules follow NIST SP 800-63B-4: long rather than complicated, no composition rules,
no forced rotation, checked against a list of common passwords. Hashing is Argon2id with
19 MiB of memory, 2 passes and 1 lane (OWASP's parameters), and at most two hashes run
at once, so a burst of logins can't exhaust a small host's memory.
"""

import asyncio
import threading
import unicodedata
from collections.abc import Callable
from functools import cache
from importlib import resources

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_LENGTH = 15
"""For an account with a password only (NIST: 15; 8 with a second factor)."""
MAX_LENGTH = 256

HASHER = PasswordHasher(time_cost=2, memory_cost=19 * 1024, parallelism=1)
_hashing = threading.BoundedSemaphore(2)


class WeakPassword(ValueError):
    pass


def normalize(password: str) -> str:
    return unicodedata.normalize("NFKC", password)


@cache
def _common() -> frozenset[str]:
    text = resources.files(__package__).joinpath("data", "common-passwords.txt").read_text()
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


def check(password: str, *, user: str) -> None:
    """Raise WeakPassword, saying why, for a password that may not be used."""
    p = normalize(password)
    if len(p) < MIN_LENGTH:
        raise WeakPassword(f"a password needs at least {MIN_LENGTH} characters")
    if len(p) > MAX_LENGTH:
        raise WeakPassword(f"a password can have at most {MAX_LENGTH} characters")
    lowered = p.lower()
    if lowered in _common() or "thermaestro" in lowered or user.lower() in lowered:
        raise WeakPassword(
            "that password is too easy to guess: it is a common one, or contains the user"
            " name or the product's"
        )


async def hash_password(password: str, hasher: PasswordHasher = HASHER) -> str:
    return await asyncio.to_thread(_limited, hasher.hash, normalize(password))


async def verify(stored: str, password: str, hasher: PasswordHasher = HASHER) -> bool:
    try:
        return bool(await asyncio.to_thread(_limited, hasher.verify, stored, normalize(password)))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _limited[T](fn: Callable[..., T], *args: str) -> T:
    with _hashing:
        return fn(*args)


def needs_rehash(stored: str, hasher: PasswordHasher = HASHER) -> bool:
    return hasher.check_needs_rehash(stored)
