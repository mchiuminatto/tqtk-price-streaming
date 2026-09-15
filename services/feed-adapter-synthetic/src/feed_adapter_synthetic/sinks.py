"""The production `TickSink`: `XADD` onto the tick's own raw-tick stream.

Per the `synthetic-feed` capability's "Raw-tick-only publication" requirement, this is the only
stream a `Tick` is ever written to - `tick_stream` derives the name from the record itself, so
there is nowhere else for a call site to accidentally send one.
"""

from __future__ import annotations

from redis.asyncio import Redis

from tqtk_common.names import tick_stream
from tqtk_common.records import Tick

__all__ = ["RedisTickSink"]


class RedisTickSink:
    """Publishes a `Tick` to `ticks.raw.{provider}.{symbol}` via `XADD`."""

    __slots__ = ("_redis",)

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def publish(self, tick: Tick) -> None:
        await self._redis.xadd(tick_stream(tick.provider, tick.symbol), tick.to_wire())
