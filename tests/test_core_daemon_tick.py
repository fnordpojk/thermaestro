"""The loop that marks sensors stale goes on whatever happens in one of its rounds."""

import asyncio
import contextlib
from typing import Any, cast

from thermaestro.core.daemon import _tick


class Hub:
    def __init__(self) -> None:
        self.ticks = 0

    def tick(self) -> None:
        self.ticks += 1
        if self.ticks == 1:
            raise RuntimeError("a listener broke")

    async def save(self) -> None:
        pass


async def test_a_failing_round_doesnt_end_the_ticker() -> None:
    hub = Hub()
    task = asyncio.create_task(_tick(cast(Any, hub), every_s=0.01))
    try:
        async with asyncio.timeout(5):
            while hub.ticks < 3:
                assert not task.done(), task
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
