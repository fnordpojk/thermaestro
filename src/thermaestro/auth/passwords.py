"""Passwords: the rules for choosing one, and hashing.

The rules follow NIST SP 800-63B-4: long rather than complicated, no composition rules,
no forced rotation, checked against lists of common and breached passwords, and against
the usual ways of padding a short one out to length. Hashing is Argon2id with
19 MiB of memory, 2 passes and 1 lane (OWASP's parameters), and at most two hashes run
at once, so a burst of logins can't exhaust a small host's memory.
"""

import asyncio
import itertools
import re
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


def _load(name: str) -> frozenset[str]:
    text = resources.files(__package__).joinpath("data", name).read_text(encoding="utf-8")
    return frozenset(line.strip().lower() for line in text.splitlines() if line.strip())


@cache
def _common() -> frozenset[str]:
    """The 10,000 most common passwords: refused as they are, and with padding."""
    return _load("common-passwords.txt")


@cache
def _breached() -> frozenset[str]:
    """Breached passwords of 15 characters or more, refused as they are."""
    return _load("breached-long-passwords.txt")


_PADDING = re.compile(r"^[\W\d_]+|[\W\d_]+$")


def _sequential(text: str) -> bool:
    """Mostly a run up or down the alphabet or the digits, such as 1234567890123456."""
    steps = [ord(b) - ord(a) for a, b in itertools.pairwise(text)]
    runs = sum(1 for s in steps if s in (1, -1, 9, -9))  # 9 and -9: from 9 to 0 and back
    return runs >= 0.8 * len(steps)


def check(password: str, *, user: str) -> None:
    """Raise WeakPassword, saying why, for a password that may not be used. There are no
    composition rules: only length, and whether it is easy to guess."""
    p = normalize(password)
    if len(p) < MIN_LENGTH:
        raise WeakPassword(f"a password needs at least {MIN_LENGTH} characters")
    if len(p) > MAX_LENGTH:
        raise WeakPassword(f"a password can have at most {MAX_LENGTH} characters")
    lowered = p.lower()
    core = _PADDING.sub("", lowered)
    if (
        lowered in _common()
        or lowered in _breached()
        or core in _common()  # a common one with digits or symbols around it
        or len(set(lowered)) < 5  # a repeat, such as aaaaaaaaaaaaaaa or 121212121212121
        or _sequential(lowered)
        or "thermaestro" in lowered
        or user.lower() in lowered
    ):
        raise WeakPassword(
            "that password is too easy to guess: it is a common one, a common one with"
            " digits or symbols around it, a repeat or a run, or contains the user name or"
            " the product's"
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
