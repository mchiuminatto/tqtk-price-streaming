"""Verification that `RedisTickSink.publish` XADDs a tick onto its own raw-tick stream, per the
`synthetic-feed` capability's "Raw-tick-only publication" requirement.
"""

from __future__ import annotations

import asyncio

from feed_adapter_synthetic.sinks import RedisTickSink

from tqtk_common.names import tick_stream
from tqtk_common.records import Tick


class _FakeRedis:
    """A `redis.asyncio.Redis` stand-in: records every `xadd` call, sends nothing anywhere."""

    def __init__(self) -> None:
        self.xadd_calls: list[tuple[str, dict[str, str]]] = []

    async def xadd(self, stream: str, fields: dict[str, str]) -> None:
        self.xadd_calls.append((stream, fields))


def _tick(**overrides: object) -> Tick:
    fields: dict[str, object] = {
        "provider": "synthetic",
        "symbol": "EURUSD",
        "provider_ts": 1_700_000_000_000_000,
        "recv_ts": 1_700_000_000_000_000,
        "bid": 1.10000,
        "ask": 1.10020,
        "session_id": "a" * 32,
        "seq": 0,
    }
    fields.update(overrides)
    return Tick(**fields)


def test_publish_xadds_onto_the_ticks_own_raw_tick_stream():
    redis = _FakeRedis()
    sink = RedisTickSink(redis)
    tick = _tick()

    asyncio.run(sink.publish(tick))

    assert len(redis.xadd_calls) == 1
    stream, _ = redis.xadd_calls[0]
    assert stream == tick_stream(tick.provider, tick.symbol)
    assert stream == "ticks.raw.synthetic.EURUSD"


def test_publish_sends_the_ticks_wire_format_as_the_stream_entry():
    redis = _FakeRedis()
    sink = RedisTickSink(redis)
    tick = _tick(bid=1.23456, ask=1.23476, seq=7)

    asyncio.run(sink.publish(tick))

    _, fields = redis.xadd_calls[0]
    assert fields == tick.to_wire()
    assert fields["bid"] == "1.23456"
    assert fields["seq"] == "7"


def test_publish_routes_different_symbols_to_different_streams():
    redis = _FakeRedis()
    sink = RedisTickSink(redis)

    asyncio.run(sink.publish(_tick(symbol="EURUSD")))
    asyncio.run(sink.publish(_tick(symbol="USDJPY")))

    streams = [stream for stream, _ in redis.xadd_calls]
    assert streams == ["ticks.raw.synthetic.EURUSD", "ticks.raw.synthetic.USDJPY"]
