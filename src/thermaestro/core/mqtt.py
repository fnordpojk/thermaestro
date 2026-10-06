"""The MQTT client: reads the topics of MQTT sensors from the broker in the settings.

One client for the whole core; the outward side (Home Assistant discovery, values for
others) joins it later. It connects only while there is a broker set and a sensor to
read, reconnects after a growing pause, and starts over when the settings change.
"""

import asyncio
import contextlib
import logging
import ssl
from typing import Literal

import aiomqtt

from ..store import Database, Mqtt, SecretStore
from .sensors import SensorHub

log = logging.getLogger(__name__)

State = Literal["off", "connecting", "connected", "failed"]


class MqttInput:
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
            if setting is None or not setting.enabled or not topics:
                self.state, self.error = "off", None
                await self._reload.wait()
                continue
            self.state = "connecting"
            try:
                await self._session(setting, topics)
                failures = 0
            except aiomqtt.MqttError as e:
                failures += 1
                self.state, self.error = "failed", str(e)
                pause = min(self._backoff[1], self._backoff[0] * 2 ** (failures - 1))
                log.warning("MQTT broker %s: %s; trying again in %.0f s", setting.host, e, pause)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._reload.wait(), pause)

    async def _session(self, setting: Mqtt, topics: list[str]) -> None:
        password = None
        if setting.password:
            secret = await self._secrets.get(setting.password)
            password = secret.get_secret_value() if secret else None
        tls = aiomqtt.TLSParameters(cert_reqs=ssl.CERT_REQUIRED) if setting.tls else None
        async with aiomqtt.Client(
            setting.host,
            setting.port,
            username=setting.username,
            password=password,
            tls_params=tls,
            identifier=None,
        ) as client:
            for topic in topics:
                await client.subscribe(topic)
            self.state, self.error = "connected", None
            log.info("MQTT broker %s: reading %d topics", setting.host, len(topics))
            messages = asyncio.create_task(self._consume(client))
            reload = asyncio.create_task(self._reload.wait())
            try:
                await asyncio.wait({messages, reload}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (messages, reload):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task
            if messages.done() and not messages.cancelled():
                messages.result()  # the broker went away: MqttError

    async def _consume(self, client: aiomqtt.Client) -> None:
        async for message in client.messages:
            payload = message.payload
            if isinstance(payload, bytes | bytearray):
                data = bytes(payload)
            elif payload is None:
                data = b""
            else:
                data = str(payload).encode()
            self._hub.receive_mqtt(str(message.topic), data)
