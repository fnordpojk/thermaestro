"""Users, their rights, sessions and API tokens.

Every check happens here, in the service layer, whichever way a request came in: the
web UI, the API, or the command line. Nothing is allowed unless a right says so.

Logins: a wrong password delays the next try on that account (1 s, doubling, at most
15 min); after 100 in a row the account is disabled until reset on the host. A client
address gets a limited number of tries in a window. An unknown user and a wrong password
look and take the same.

Sessions and tokens are random 256-bit values; only their SHA-256 is stored.
"""

import hashlib
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from typing import NoReturn

from argon2 import PasswordHasher

from ..core.audit import AuditLog
from ..store import Database, Transaction
from . import passwords
from .permissions import STEP_UP, allows, known
from .setup import SetupCode

log = logging.getLogger(__name__)

SESSION_IDLE_S = 12 * 3600.0
SESSION_MAX_S = 7 * 86_400.0
TOUCH_S = 60.0
STEP_UP_S = 900.0
"""A password entered this long ago still counts for changes that need it again."""
"""A session's or token's last use is written at most this often."""
TOKEN_PREFIX = "thm_"  # noqa: S105 - marks a token, isn't one
TOKEN_DAYS = 365
DELAY_BASE_S = 1.0
DELAY_MAX_S = 900.0
DISABLE_AFTER = 100
NAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


class AccountError(Exception):
    """A refusal, with a message fit to show."""


class LoginFailed(AccountError):
    def __init__(self) -> None:
        super().__init__("wrong user name or password")


class Forbidden(AccountError):
    def __init__(self, permission: str) -> None:
        super().__init__(f"not allowed: needs {permission}")
        self.permission = permission


@dataclass(frozen=True, slots=True)
class User:
    id: int
    name: str
    groups: tuple[str, ...]
    permissions: frozenset[str]
    """Its own and its groups', together."""
    disabled: bool = False


@dataclass(frozen=True, slots=True)
class Principal:
    """Who a request acts as: a user, and for a token, the rights the token carries."""

    user: User
    scope: frozenset[str] | None = None

    @property
    def name(self) -> str:
        return f"user:{self.user.name}" if self.scope is None else f"token:{self.user.name}"

    def allows(self, permission: str) -> bool:
        if not allows(self.user.permissions, permission):
            return False
        return self.scope is None or allows(self.scope, permission)

    def require(self, permission: str) -> None:
        if not self.allows(permission):
            raise Forbidden(permission)


@dataclass(frozen=True, slots=True)
class Session:
    hash: str
    user: User
    created: float
    expires: float
    confirmed: float
    """When the password was last entered: at login, or again for a step-up."""

    @property
    def id(self) -> str:
        """Names the session in lists; the hash's start, which can't log anyone in."""
        return self.hash[:16]


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: str
    created: float
    last_seen: float
    source: str | None
    agent: str | None


@dataclass(frozen=True, slots=True)
class TokenInfo:
    id: int
    name: str
    permissions: tuple[str, ...]
    created: float
    expires: float
    last_used: float | None


def digest(raw: str) -> str:
    """How a session or token is stored: its SHA-256."""
    return hashlib.sha256(raw.encode()).hexdigest()


class AddressLimiter:
    """At most `tries` login attempts per client address in a sliding window."""

    def __init__(self, tries: int = 30, window_s: float = 300.0) -> None:
        self.tries = tries
        self.window_s = window_s
        self._seen: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, address: str, now: float) -> bool:
        if len(self._seen) > 10_000:  # forget addresses whose window has passed
            for old in [a for a, s in self._seen.items() if not s or now - s[-1] > self.window_s]:
                del self._seen[old]
        seen = self._seen[address]
        while seen and now - seen[0] > self.window_s:
            seen.popleft()
        if len(seen) >= self.tries:
            return False
        seen.append(now)
        return True


class Accounts:
    def __init__(
        self,
        db: Database,
        audit: AuditLog,
        *,
        hasher: PasswordHasher = passwords.HASHER,
        clock: Callable[[], float] = time.time,
        limiter: AddressLimiter | None = None,
    ) -> None:
        self._db = db
        self._audit = audit
        self._hasher = hasher
        self._clock = clock
        self._limiter = limiter or AddressLimiter()
        self._dummy: str | None = None

    # --- users ---------------------------------------------------------------------------

    async def has_admin(self) -> bool:
        return await self._db.run(_has_admin)

    async def users(self) -> list[User]:
        return await self._db.run(lambda t: [_user(t, row) for row in _rows(t, None)])

    async def user(self, name: str) -> User | None:
        def find(t: Transaction) -> User | None:
            rows = _rows(t, name)
            return _user(t, rows[0]) if rows else None

        return await self._db.run(find)

    async def groups(self) -> dict[str, frozenset[str]]:
        def read(t: Transaction) -> dict[str, frozenset[str]]:
            out: dict[str, set[str]] = {g: set() for (g,) in t.execute("SELECT name FROM groups")}
            for g, p in t.execute("SELECT group_name, permission FROM group_permissions"):
                out[g].add(p)
            return {g: frozenset(p) for g, p in out.items()}

        return await self._db.run(read)

    async def set_group(
        self, name: str, permissions: Collection[str], *, by: str, source: str | None = None
    ) -> None:
        """Create the group, or replace its rights."""
        if not NAME.match(name):
            raise AccountError("a group name is 1 to 64 letters, digits or . _ @ -")
        for p in permissions:
            if not known(p):
                raise AccountError(f"no such right: {p}")

        def update(t: Transaction) -> None:
            had = _has_admin(t)
            t.execute("INSERT OR IGNORE INTO groups (name) VALUES (?)", (name,))
            t.execute("DELETE FROM group_permissions WHERE group_name = ?", (name,))
            t.executemany(
                "INSERT INTO group_permissions (group_name, permission) VALUES (?, ?)",
                [(name, p) for p in permissions],
            )
            _keep_an_admin(t, had)

        await self._db.run(update)
        await self._audit.record(
            by, "group.set", source=source, details={"group": name, "rights": sorted(permissions)}
        )

    async def delete_group(self, name: str, *, by: str, source: str | None = None) -> None:
        def delete(t: Transaction) -> None:
            had = _has_admin(t)
            if t.execute("DELETE FROM groups WHERE name = ?", (name,)).rowcount == 0:
                raise AccountError(f"no group {name!r}")
            _keep_an_admin(t, had)

        await self._db.run(delete)
        await self._audit.record(by, "group.delete", source=source, details={"group": name})

    async def create_user(
        self,
        name: str,
        password: str,
        groups: Collection[str] = (),
        *,
        by: str,
        source: str | None = None,
    ) -> User:
        if not NAME.match(name):
            raise AccountError("a user name is 1 to 64 letters, digits or . _ @ -")
        try:
            passwords.check(password, user=name)
        except passwords.WeakPassword as e:
            raise AccountError(str(e)) from None
        stored = await passwords.hash_password(password, self._hasher)
        now = self._clock()

        def create(t: Transaction) -> User:
            if t.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise AccountError(f"there is already a user {name!r}")
            _check_groups(t, groups)
            user_id = t.execute(
                "INSERT INTO users (name, password, created) VALUES (?, ?, ?)",
                (name, stored, now),
            ).lastrowid
            t.executemany(
                "INSERT INTO user_groups (user_id, group_name) VALUES (?, ?)",
                [(user_id, g) for g in groups],
            )
            return _user(t, _rows(t, name)[0])

        user = await self._db.run(create)
        await self._audit.record(
            by, "user.create", source=source, details={"user": name, "groups": sorted(groups)}
        )
        return user

    async def create_first_admin(
        self, setup: SetupCode, code: str, name: str, password: str, *, source: str
    ) -> User:
        """The first administrator, from the web UI: only with the setup code, and only
        while no user can manage users."""
        if not self._limiter.allow(source, self._clock()):
            raise AccountError("too many attempts; wait a few minutes")
        if await self.has_admin():
            raise AccountError("there is already an administrator")
        if not NAME.match(name):
            raise AccountError("a user name is 1 to 64 letters, digits or . _ @ -")
        try:
            passwords.check(password, user=name)  # before the code is used up
        except passwords.WeakPassword as e:
            raise AccountError(str(e)) from None
        if not setup.redeem(code):
            log.warning("wrong setup code from %s", source)
            await self._audit.record(
                "anonymous", "setup", outcome="failed", why="wrong code", source=source
            )
            raise AccountError("the setup code isn't right, or has expired")
        user = await self.create_user(
            name, password, ["Administrators"], by="anonymous", source=source
        )
        await self._audit.record(f"user:{name}", "setup", source=source)
        return user

    async def set_groups(
        self, name: str, groups: Collection[str], *, by: str, source: str | None = None
    ) -> None:
        def update(t: Transaction) -> None:
            had = _has_admin(t)
            user = _user_id(t, name)
            _check_groups(t, groups)
            t.execute("DELETE FROM user_groups WHERE user_id = ?", (user,))
            t.executemany(
                "INSERT INTO user_groups (user_id, group_name) VALUES (?, ?)",
                [(user, g) for g in groups],
            )
            _keep_an_admin(t, had)

        await self._db.run(update)
        await self._audit.record(
            by, "user.groups", source=source, details={"user": name, "groups": sorted(groups)}
        )

    async def set_password(
        self,
        name: str,
        password: str,
        *,
        by: str,
        source: str | None = None,
        end_sessions: bool = True,
    ) -> None:
        try:
            passwords.check(password, user=name)
        except passwords.WeakPassword as e:
            raise AccountError(str(e)) from None
        stored = await passwords.hash_password(password, self._hasher)

        def update(t: Transaction) -> None:
            user = _user_id(t, name)
            t.execute(
                "UPDATE users SET password = ?, failures = 0, locked_until = 0, disabled = 0"
                " WHERE id = ?",
                (stored, user),
            )
            if end_sessions:
                t.execute("DELETE FROM sessions WHERE user_id = ?", (user,))

        await self._db.run(update)
        await self._audit.record(
            by,
            "user.password",
            source=source,
            details={"user": name, "sessions_ended": end_sessions},
        )

    async def delete_user(self, name: str, *, by: str, source: str | None = None) -> None:
        def delete(t: Transaction) -> None:
            had = _has_admin(t)
            user = _user_id(t, name)
            t.execute("DELETE FROM users WHERE id = ?", (user,))
            _keep_an_admin(t, had)

        await self._db.run(delete)
        await self._audit.record(by, "user.delete", source=source, details={"user": name})

    async def confirm(self, session: Session, password: str, *, source: str) -> None:
        """The password entered again, for changes that need it (STEP_UP). Wrong ones count
        against the address like failed logins."""
        if not self._limiter.allow(source, self._clock()):
            raise AccountError("too many attempts; wait a few minutes")
        stored = await self._db.run(
            lambda t: t.execute(
                "SELECT password FROM users WHERE id = ?", (session.user.id,)
            ).fetchone()
        )
        if stored is None or not await passwords.verify(stored[0], password, self._hasher):
            await self._audit.record(
                f"user:{session.user.name}", "confirm", outcome="failed", source=source
            )
            raise AccountError("the password you entered isn't right")
        now = self._clock()
        await self._db.run(
            lambda t: t.execute(
                "UPDATE sessions SET confirmed = ? WHERE hash = ?", (now, session.hash)
            )
        )

    def confirmed_recently(self, session: Session) -> bool:
        return self._clock() - session.confirmed < STEP_UP_S

    # --- logging in ----------------------------------------------------------------------

    async def authenticate(self, name: str, password: str, *, source: str) -> User:
        now = self._clock()
        if not self._limiter.allow(source, now):
            log.warning("login refused from %s: too many attempts", source)
            await self._audit.record(
                "anonymous", "login", outcome="refused", why="too many attempts", source=source
            )
            raise LoginFailed
        row = await self._db.run(
            lambda t: t.execute(
                "SELECT id, password, failures, locked_until, disabled FROM users WHERE name = ?",
                (name,),
            ).fetchone()
        )
        if row is None or row[4] or now < row[3]:
            await passwords.verify(await self._dummy_hash(), password, self._hasher)  # same time
            why = "unknown" if row is None else "disabled" if row[4] else "delayed"
            await self._failed(name, source, why)
        user_id, stored, failures, _, _ = row
        if not await passwords.verify(stored, password, self._hasher):
            failures += 1
            delay = min(DELAY_MAX_S, DELAY_BASE_S * 2 ** (failures - 1))
            await self._db.run(
                lambda t: t.execute(
                    "UPDATE users SET failures = ?, locked_until = ?, disabled = ? WHERE id = ?",
                    (failures, now + delay, int(failures >= DISABLE_AFTER), user_id),
                )
            )
            await self._failed(name, source, "password")
        rehash = passwords.needs_rehash(stored, self._hasher)
        new_hash = await passwords.hash_password(password, self._hasher) if rehash else stored
        await self._db.run(
            lambda t: t.execute(
                "UPDATE users SET failures = 0, locked_until = 0, password = ? WHERE id = ?",
                (new_hash, user_id),
            )
        )
        await self._audit.record(f"user:{name}", "login", source=source)
        user = await self.user(name)
        if user is None:  # removed in the meantime
            raise LoginFailed
        return user

    async def _failed(self, name: str, source: str, why: str) -> NoReturn:
        # One stable line per failure, for fail2ban and the like.
        log.warning("login failed for %r from %s", name, source)
        await self._audit.record(
            "anonymous", "login", outcome="failed", why=why, source=source, details={"user": name}
        )
        raise LoginFailed

    async def _dummy_hash(self) -> str:
        if self._dummy is None:
            self._dummy = await passwords.hash_password(secrets.token_hex(16), self._hasher)
        return self._dummy

    # --- sessions ------------------------------------------------------------------------

    async def start_session(
        self, user: User, *, source: str | None = None, agent: str | None = None
    ) -> str:
        raw = secrets.token_urlsafe(32)
        now = self._clock()
        await self._db.run(
            lambda t: t.execute(
                "INSERT INTO sessions"
                " (hash, user_id, created, last_seen, expires, confirmed, source, agent)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    digest(raw),
                    user.id,
                    now,
                    now,
                    now + SESSION_MAX_S,
                    now,
                    source,
                    (agent or "")[:200],
                ),
            )
        )
        return raw

    async def session(self, raw: str) -> Session | None:
        key = digest(raw)
        now = self._clock()

        def find(t: Transaction) -> Session | None:
            row = t.execute(
                "SELECT user_id, created, last_seen, expires, confirmed FROM sessions"
                " WHERE hash = ?",
                (key,),
            ).fetchone()
            if row is None:
                return None
            user_id, created, last_seen, expires, confirmed = row
            if now >= expires or now - last_seen >= SESSION_IDLE_S:
                t.execute("DELETE FROM sessions WHERE hash = ?", (key,))
                return None
            users = _rows_by_id(t, user_id)
            if not users or users[0][2]:
                return None
            if now - last_seen >= TOUCH_S:
                t.execute("UPDATE sessions SET last_seen = ? WHERE hash = ?", (now, key))
            return Session(key, _user(t, users[0]), created, expires, confirmed)

        return await self._db.run(find)

    async def end_session(self, raw: str) -> None:
        await self._db.run(
            lambda t: t.execute("DELETE FROM sessions WHERE hash = ?", (digest(raw),))
        )

    async def sessions(self, user: User) -> list[SessionInfo]:
        """The user's sessions that are still valid, newest first."""
        now = self._clock()
        rows = await self._db.run(
            lambda t: t.execute(
                "SELECT hash, created, last_seen, source, agent FROM sessions"
                " WHERE user_id = ? AND expires > ? AND last_seen > ? ORDER BY created DESC",
                (user.id, now, now - SESSION_IDLE_S),
            ).fetchall()
        )
        return [SessionInfo(h[:16], c, seen, s, a) for h, c, seen, s, a in rows]

    async def end_session_by_id(
        self, principal: Principal, user: str, session_id: str, *, source: str | None = None
    ) -> None:
        """End one listed session: one's own, or with users.manage, anyone's."""
        if user.lower() != principal.user.name.lower():
            principal.require("users.manage")
        if not re.fullmatch(r"[0-9a-f]{16}", session_id):
            raise AccountError("no such session")

        def end(t: Transaction) -> None:
            removed = t.execute(
                "DELETE FROM sessions WHERE user_id = ? AND substr(hash, 1, 16) = ?",
                (_user_id(t, user), session_id),
            ).rowcount
            if not removed:
                raise AccountError("no such session")

        await self._db.run(end)
        await self._audit.record(
            principal.name, "session.end", source=source, details={"user": user}
        )

    async def end_sessions(self, name: str, *, by: str, source: str | None = None) -> None:
        await self._db.run(
            lambda t: t.execute("DELETE FROM sessions WHERE user_id = ?", (_user_id(t, name),))
        )
        await self._audit.record(by, "user.sessions_ended", source=source, details={"user": name})

    # --- API tokens ----------------------------------------------------------------------

    async def create_token(
        self,
        principal: Principal,
        name: str,
        permissions: Iterable[str],
        *,
        days: int = TOKEN_DAYS,
        source: str | None = None,
    ) -> str:
        """A new token for the principal's own user, carrying at most its rights."""
        principal.require("tokens.own")
        wanted = frozenset(permissions)
        for p in wanted:
            if not known(p):
                raise AccountError(f"no such right: {p}")
            if not principal.allows(p):
                raise AccountError(f"a token can't carry {p}, which you don't have")
        if not 1 <= days <= 3650:
            raise AccountError("a token lasts 1 to 3650 days")
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = self._clock()
        await self._db.run(
            lambda t: t.execute(
                "INSERT INTO tokens (hash, user_id, name, permissions, created, expires)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    digest(raw),
                    principal.user.id,
                    name[:64],
                    json.dumps(sorted(wanted)),
                    now,
                    now + days * 86_400,
                ),
            )
        )
        await self._audit.record(
            principal.name,
            "token.create",
            source=source,
            details={"name": name, "permissions": sorted(wanted), "days": days},
        )
        return raw

    async def tokens(self, user: User) -> list[TokenInfo]:
        def read(t: Transaction) -> list[TokenInfo]:
            rows = t.execute(
                "SELECT id, name, permissions, created, expires, last_used FROM tokens"
                " WHERE user_id = ? ORDER BY created",
                (user.id,),
            )
            return [
                TokenInfo(i, n, tuple(json.loads(p)), c, e, last) for i, n, p, c, e, last in rows
            ]

        return await self._db.run(read)

    async def revoke_token(
        self, principal: Principal, token_id: int, *, source: str | None = None
    ) -> None:
        """Revoke one's own token, or, with users.manage, anyone's."""

        def revoke(t: Transaction) -> None:
            row = t.execute("SELECT user_id FROM tokens WHERE id = ?", (token_id,)).fetchone()
            if row is None or (
                row[0] != principal.user.id and not principal.allows("users.manage")
            ):
                raise AccountError("no such token")
            t.execute("DELETE FROM tokens WHERE id = ?", (token_id,))

        await self._db.run(revoke)
        await self._audit.record(
            principal.name, "token.revoke", source=source, details={"token": token_id}
        )

    async def principal_for_token(self, raw: str) -> Principal | None:
        if not raw.startswith(TOKEN_PREFIX):
            return None
        key = digest(raw)
        now = self._clock()

        def find(t: Transaction) -> Principal | None:
            row = t.execute(
                "SELECT user_id, permissions, expires, last_used FROM tokens WHERE hash = ?", (key,)
            ).fetchone()
            if row is None or now >= row[2]:
                return None
            users = _rows_by_id(t, row[0])
            if not users or users[0][2]:
                return None
            if row[3] is None or now - row[3] >= TOUCH_S:
                t.execute("UPDATE tokens SET last_used = ? WHERE hash = ?", (now, key))
            return Principal(_user(t, users[0]), frozenset(json.loads(row[1])))

        return await self._db.run(find)


def needs_step_up(permission: str) -> bool:
    return permission in STEP_UP


def _has_admin(t: Transaction) -> bool:
    """Whether some enabled user can manage users."""
    users = (_user(t, row) for row in _rows(t, None))
    return any(not u.disabled and allows(u.permissions, "users.manage") for u in users)


def _keep_an_admin(t: Transaction, had: bool) -> None:
    """Refuse a change that takes away the last way to manage users."""
    if had and not _has_admin(t):
        raise AccountError("that would leave no user who can manage users")


def _rows(t: Transaction, name: str | None) -> list[tuple[int, str, int]]:
    if name is None:
        return list(t.execute("SELECT id, name, disabled FROM users ORDER BY name"))
    return list(t.execute("SELECT id, name, disabled FROM users WHERE name = ?", (name,)))


def _rows_by_id(t: Transaction, user_id: int) -> list[tuple[int, str, int]]:
    return list(t.execute("SELECT id, name, disabled FROM users WHERE id = ?", (user_id,)))


def _user(t: Transaction, row: tuple[int, str, int]) -> User:
    user_id, name, disabled = row
    groups = tuple(
        g for (g,) in t.execute("SELECT group_name FROM user_groups WHERE user_id = ?", (user_id,))
    )
    granted = {
        p
        for (p,) in t.execute(
            "SELECT permission FROM user_permissions WHERE user_id = ?"
            " UNION SELECT gp.permission FROM group_permissions gp"
            " JOIN user_groups ug ON ug.group_name = gp.group_name WHERE ug.user_id = ?",
            (user_id, user_id),
        )
    }
    return User(user_id, name, groups, frozenset(granted), bool(disabled))


def _user_id(t: Transaction, name: str) -> int:
    row = t.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise AccountError(f"no user {name!r}")
    return int(row[0])


def _check_groups(t: Transaction, groups: Collection[str]) -> None:
    existing = {g for (g,) in t.execute("SELECT name FROM groups")}
    unknown = set(groups) - existing
    if unknown:
        raise AccountError(f"no such group: {', '.join(sorted(unknown))}")
