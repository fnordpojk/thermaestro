"""The MQTT client: one connection to the broker in the settings, for the whole core.

It reads the topics of MQTT sensors, and carries the outward side (Home Assistant
discovery) when that is on. It connects only while there is something to do, reconnects
after a growing pause, and starts over when the settings change.
"""

import asyncio
import contextlib
import logging
import ssl
from typing import Literal, Protocol

import aiomqtt

from ..store import Database, Mqtt, SecretStore
from .sensors import SensorHub

log = logging.getLogger(__name__)

State = Literal["off", "connecting", "connected", "failed"]
Wanted = Literal["publish", "sweep"] | None


class Outward(Protocol):
    """What publishes through the client: Home Assistant discovery."""

    async def wanted(self) -> Wanted:
        """`publish` while it is on; `sweep` to clear what an earlier run left at the
        broker; None for nothing."""
        ...

    def will(self) -> aiomqtt.Will | None:
        """The message the broker sends for us if the connection is lost."""
        ...

    def handles(self, topic: str) -> bool: ...

    def receive(self, topic: str, payload: bytes, retained: bool) -> None: ...

    async def session(self, client: aiomqtt.Client, wanted: Wanted) -> None:
        """Publish until cancelled, or sweep and return."""
        ...


class MqttClient:
    def __init__(
        self,
        db: Database,
        secrets: SecretStore,
        hub: SensorHub,
        *,
        backoff_s: tuple[float, float] = (1.0, 60.0),
    ) -> None:
        self._db = db
        self._secrets = secrets
        self._hub = hub
        self._backoff = backoff_s
        self._reload = asyncio.Event()
        self.outward: Outward | None = None
        self.state: State = "off"
        self.error: str | None = None

    def reload(self) -> None:
        """Start over with the settings and sensors as they are now."""
        self._reload.set()

    async def run(self) -> None:
        failures = 0
        while True:
            self._reload.clear()
            setting = await self._db.get(Mqtt)
            topics = self._hub.topics()
            wanted = await self.outward.wanted() if self.outward is not None else None
            if setting is None or not setting.enabled or not (topics or wanted):
                self.state, self.error = "off", None
                await self._reload.wait()
                continue
            self.state = "connecting"
            try:
                await self._session(setting, topics, wanted)
                failures = 0
            except aiomqtt.MqttError as e:
                failures += 1
                self.state, self.error = "failed", str(e)
                pause = min(self._backoff[1], self._backoff[0] * 2 ** (failures - 1))
                log.warning("MQTT broker %s: %s; trying again in %.0f s", setting.host, e, pause)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._reload.wait(), pause)

    async def _session(self, setting: Mqtt, topics: list[str], wanted: Wanted) -> None:
        password = None
        if setting.password:
            secret = await self._secrets.get(setting.password)
            password = secret.get_secret_value() if secret else None
        tls = aiomqtt.TLSParameters(cert_reqs=ssl.CERT_REQUIRED) if setting.tls else None
        outward = self.outward if wanted else None
        async with aiomqtt.Client(
            setting.host,
            setting.port,
            username=setting.username,
            password=password,
            tls_params=tls,
            identifier=None,
            will=outward.will() if outward is not None and wanted == "publish" else None,
        ) as client:
            for topic in topics:
                await client.subscribe(topic)
            self.state, self.error = "connected", None
            log.info("MQTT broker %s: reading %d topics", setting.host, len(topics))
            tasks = {
                asyncio.create_task(self._consume(client)),
                asyncio.create_task(self._reload.wait()),
            }
            if outward is not None:
                tasks.add(asyncio.create_task(outward.session(client, wanted)))
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            for task in done:
                if not task.cancelled():
                    task.result()  # the broker went away: MqttError

    async def _consume(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            payload = message.payload
            if isinstance(payload, bytes | bytearray):
                data = bytes(payload)
            elif payload is None:
                data = b""
            else:
                data = str(payload).encode()
            topic = str(message.topic)
            if self.outward is not None and self.outward.handles(topic):
                self.outward.receive(topic, data, bool(message.retain))
            else:
                self._hub.receive_mqtt(topic, data)
