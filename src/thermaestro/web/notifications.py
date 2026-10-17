"""Notifications: what a person turned on, sent where they chose, in their language.

Nothing is sent unless a user turned it on, and only what their rights let them see: their
own requests (and, with the right to end anyone's, everyone's), rooms below their band and
the daily summary for those who may see the plan, the house's workings for those who may
read the settings. They go to a Home Assistant notification target the user picked, sent
through that Home Assistant's plugin, and to an MQTT topic of their own while discovery is
on.

Each event is sent as it is raised, again as what it says changes, and as it clears; a
moment, over as it happens, once. The daily summary's carries the summary itself, as the
user may read it. At most `LIMIT` an hour to each person: beyond, they are held back, and
the Events page has them. Every one sent, failed or held back is kept for 30 days, and
the user's page shows what failed.
"""

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import tzinfo
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, StringConstraints

from ..auth import Accounts, User
from ..auth.permissions import allows
from ..cap.messages import TargetId
from ..core.events import Event, Events
from ..store import Database
from . import i18n
from .labels import summary_lines

if TYPE_CHECKING:
    from ..core.discovery import Publisher
    from ..core.host import PluginHost

log = logging.getLogger(__name__)

Condition = Literal["mine", "everyone", "rooms", "summary", "house"]
NEEDS: dict[Condition, tuple[str, ...]] = {
    "mine": ("plan.read",),
    "everyone": ("plan.read", "intent.any.end"),
    "rooms": ("plan.read",),
    "summary": ("plan.read",),
    "house": ("settings.read",),
}
"""The rights each condition needs: what the Events page shows to whom."""
LABELS: dict[Condition, str] = {
    "mine": i18n.mark("My requests: at risk, giving way or missed"),
    "everyone": i18n.mark("Everyone's requests"),
    "rooms": i18n.mark("A room below its band"),
    "summary": i18n.mark("The daily summary"),
    "house": i18n.mark("The house's workings: floors, settings let go, plugins, warnings"),
}
CHANNELS = {
    "home_assistant": "Home Assistant",
    "mqtt": "MQTT",
    "limit": i18n.mark("held back by the limit"),
}
LIMIT = 10
WINDOW_S = 3600.0
KEEP_S = 30 * 86_400.0
TIMEOUT_S = 15.0
LIST_S = 5.0
"""The longest the account page waits for a Home Assistant to list its targets."""
TITLE = "Thermaestro"
OVER = i18n.mark("Over: %(what)s")
DAY_BEFORE = i18n.mark("The day before, %(day)s")
MESSAGE_MAX = 2000
"""What a notification may say, as the plugin protocol allows."""
TEST = i18n.mark("A test notification: this is where Thermaestro's notifications reach you.")
QUEUED = 200
"""Events waiting to be sent; beyond, the newest are dropped and logged."""


class HomeAssistantTarget(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    instance: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")]
    """The Home Assistant plugin instance that sends it."""
    target: TargetId
    """One of its notify services, such as `mobile_app_pixel_8`."""


class Wanted(BaseModel):
    """What one person turned on, and where it goes. Nothing, to begin with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conditions: tuple[Condition, ...] = ()
    home_assistant: HomeAssistantTarget | None = None
    mqtt: bool = False


@dataclass(frozen=True)
class Sent:
    t: float
    channel: str
    """`home_assistant` or `mqtt`; `limit` for one held back."""
    event: str
    """The event's key, and whether it was raised, changed or cleared."""
    outcome: Literal["sent", "failed", "held"]
    detail: str | None = None


def allowed(user: User) -> list[Condition]:
    """What a user may turn on."""
    return [c for c, needs in NEEDS.items() if all(_has(user, n) for n in needs)]


def condition(event: Event, user: User) -> Condition | None:
    """Which of a user's conditions an event comes under, if any."""
    if event.kind.startswith("intent."):
        return "mine" if _asker(event.principal) == user.name.casefold() else "everyone"
    if event.kind == "comfort.missed":
        return "rooms"
    if event.kind == "summary.daily":
        return "summary"
    if event.audience == "technical":
        return "house"
    return None


class Notifier:
    def __init__(
        self,
        db: Database,
        accounts: Accounts,
        host: "PluginHost | None",
        discovery: "Publisher | None",
        zone: Callable[[], tzinfo],
        *,
        clock: Callable[[], float] = time.time,
        limit: int = LIMIT,
    ) -> None:
        self._db = db
        self._accounts = accounts
        self._host = host
        self._discovery = discovery
        self._zone = zone
        self._clock = clock
        self.limit = limit
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUED)
        self._recent: dict[int, deque[float]] = {}
        """Per user, when each of the last hour's notifications went."""

    def listen(self, events: Events) -> None:
        events.listeners.append(self._heard)

    def _heard(self, event: Event) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("notifications: too many waiting; %s not sent", event.key)

    async def run(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self.handle(event)
            except Exception:
                log.exception("notifications: %s couldn't be sent", event.key)

    async def drain(self) -> None:
        """Send whatever is waiting: for tests."""
        while not self._queue.empty():
            await self.handle(self._queue.get_nowait())

    # --- what each person wants ----------------------------------------------------------

    async def wanted(self, user: User) -> Wanted:
        row = await self._db.run(
            lambda t: t.execute(
                "SELECT body FROM notifications WHERE user_id = ?", (user.id,)
            ).fetchone()
        )
        return Wanted() if row is None else Wanted.model_validate_json(row[0])

    async def choose(self, user: User, wanted: Wanted) -> Wanted:
        """What a user turns on: only what their rights allow, to a Home Assistant that is
        set up."""
        refused = sorted(set(wanted.conditions) - set(allowed(user)))
        if refused:
            raise ValueError(f"not allowed to turn on: {', '.join(refused)}")
        ha = wanted.home_assistant
        if ha is not None:
            instance = self._host.instances.get(ha.instance) if self._host else None
            if instance is None or instance.setting.plugin != "homeassistant":
                raise ValueError(f"there is no Home Assistant {ha.instance!r}")
        kept = wanted.model_copy(update={"conditions": tuple(sorted(set(wanted.conditions)))})
        body = kept.model_dump_json(exclude_defaults=True)
        await self._db.run(
            lambda t: t.execute(
                "INSERT OR REPLACE INTO notifications (user_id, body) VALUES (?, ?)",
                (user.id, body),
            )
        )
        return kept

    async def targets(self) -> list[tuple[str, str, str | None]]:
        """Every notification target the Home Assistants offer: instance, target, name."""
        out: list[tuple[str, str, str | None]] = []
        for id, instance in sorted((self._host.instances if self._host else {}).items()):
            link = instance.link
            if link is None or instance.hello is None or "notify" not in instance.hello.features:
                continue
            try:
                listed = await link.notify_targets(timeout=LIST_S)
            except Exception as e:  # noqa: BLE001 - one Home Assistant failing hides only its own
                log.warning("notifications: %s listed no targets: %s", id, e)
                continue
            out += [(id, t.id, t.name) for t in listed.targets]
        return out

    def topic(self, user: User) -> str | None:
        """A user's MQTT topic, while discovery is on."""
        return self._discovery.notify_topic(user.name) if self._discovery else None

    async def recent(self, user: User, days: float = 7.0) -> list[Sent]:
        """What was sent to a user, failed or held back, newest first."""
        since = self._clock() - days * 86_400
        rows = await self._db.run(
            lambda t: t.execute(
                "SELECT t, channel, event, outcome, detail FROM notified"
                " WHERE user_id = ? AND t >= ? ORDER BY t DESC, id DESC LIMIT 100",
                (user.id, since),
            ).fetchall()
        )
        return [Sent(*r) for r in rows]

    # --- sending -------------------------------------------------------------------------

    async def handle(self, event: Event) -> None:
        if event.cleared is not None and event.cleared == event.raised:
            return  # a moment, told as it was raised
        phase = (
            "cleared"
            if event.cleared is not None
            else "changed"
            if event.changed > event.raised
            else "raised"
        )
        for user in await self._accounts.users():
            if user.disabled:
                continue
            wanted = await self.wanted(user)
            found = condition(event, user)
            if found is None or found not in allowed(user):
                continue
            chosen = set(wanted.conditions)
            if found not in chosen and not (found == "mine" and "everyone" in chosen):
                continue
            await self._send(user, wanted, event, phase)

    async def test(self, user: User) -> list[Sent]:
        """A test, now, to wherever the user chose; not counted against the limit."""
        wanted = await self.wanted(user)
        with self._in(user):
            text = i18n._(TEST)
        return await self._out(user, wanted, text, {"kind": "test"}, "test")

    async def _send(self, user: User, wanted: Wanted, event: Event, phase: str) -> None:
        name = f"{event.key} {phase}"
        now = self._clock()
        sent = self._recent.setdefault(user.id, deque())
        while sent and sent[0] <= now - WINDOW_S:
            sent.popleft()
        if len(sent) >= self.limit:
            await self._record(user, Sent(now, "limit", name, "held"))
            return
        sent.append(now)
        summary = await self._summary(event, user)
        with self._in(user):
            text = i18n.said(event.message, event.args)
            if summary is not None:
                day = i18n._(DAY_BEFORE, day=summary.get("day"))
                text = "\n".join([day, *summary_lines(summary)])[:MESSAGE_MAX]
            elif phase == "cleared":
                text = i18n._(OVER, what=text)
        about = {"kind": event.kind, "key": event.key, "state": phase, "t": now}
        await self._out(user, wanted, text, about, name)

    async def _summary(self, event: Event, user: User) -> dict[str, Any] | None:
        """The daily summary a `summary.daily` event tells of, as that user may read it."""
        day = event.args.get("day")
        if event.kind != "summary.daily" or not isinstance(day, str):
            return None
        row = await self._db.run(
            lambda t: t.execute("SELECT body FROM summaries WHERE day = ?", (day,)).fetchone()
        )
        if row is None:
            return None
        found: dict[str, Any] = json.loads(row[0])
        if not _has(user, "settings.read"):
            found = {k: v for k, v in found.items() if k not in ("writes", "starts")}
        return found

    async def _out(
        self, user: User, wanted: Wanted, text: str, about: dict[str, Any], name: str
    ) -> list[Sent]:
        done = []
        if wanted.home_assistant is not None:
            failed = await self._home_assistant(wanted.home_assistant, text)
            done.append(self._outcome("home_assistant", name, failed))
        if wanted.mqtt:
            payload = {"title": TITLE, "message": text, **about}
            if self._discovery is None:
                failed = "there is no MQTT connection"
            else:
                failed = await self._discovery.notify(user.name, payload)
            done.append(self._outcome("mqtt", name, failed))
        for one in done:
            if one.outcome == "failed":
                log.warning("notifications: to %s by %s: %s", user.name, one.channel, one.detail)
            await self._record(user, one)
        return done

    def _outcome(self, channel: str, name: str, failed: str | None) -> Sent:
        if failed is None:
            return Sent(self._clock(), channel, name, "sent")
        return Sent(self._clock(), channel, name, "failed", failed)

    async def _home_assistant(self, chosen: HomeAssistantTarget, text: str) -> str | None:
        """Why it couldn't be sent; None once it was."""
        instance = self._host.instances.get(chosen.instance) if self._host else None
        if instance is None:
            return f"there is no Home Assistant {chosen.instance!r}"
        link = instance.link
        if link is None or instance.hello is None:
            return f"the Home Assistant {chosen.instance!r} isn't connected"
        if "notify" not in instance.hello.features:
            return f"the Home Assistant {chosen.instance!r} doesn't send notifications"
        try:
            answer = await link.notify(chosen.target, TITLE, text, timeout=TIMEOUT_S)
        except Exception as e:  # noqa: BLE001 - a failure is kept and shown, never raised
            return str(e) or type(e).__name__
        return None if answer.ok else answer.detail or "Home Assistant refused it"

    async def _record(self, user: User, sent: Sent) -> None:
        def write(t: Any) -> None:
            t.execute(
                "INSERT INTO notified (user_id, t, channel, event, outcome, detail)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (user.id, sent.t, sent.channel, sent.event, sent.outcome, sent.detail),
            )
            t.execute("DELETE FROM notified WHERE t < ?", (sent.t - KEEP_S,))

        await self._db.run(write)

    @contextlib.contextmanager
    def _in(self, user: User) -> Iterator[None]:
        language, formats = i18n.resolve(user.preferences, None, None, self._zone())
        with i18n.using(language, formats):
            yield


def _has(user: User, permission: str) -> bool:
    return allows(user.permissions, permission)


def _asker(principal: str | None) -> str | None:
    """The user an intent's asker names: `user:alice`, `token:alice`, `display:alice`."""
    if principal is None or ":" not in principal:
        return None
    via, name = principal.split(":", 1)
    return name.casefold() if via in ("user", "token", "display") else None
