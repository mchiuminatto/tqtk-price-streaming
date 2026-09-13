"""Verification for tasks 5.6 and 5.7: the `__main__` entrypoint wiring.

`RuntimeServer`/`Readiness`'s own `/health`, `/ready`, `/metrics` contract is exhaustively tested
in `tqtk_common` - these tests verify this service's composition (the right dependency name, the
right host/port, the right config field feeding `pacing_multiplier`) rather than re-proving that
contract. `RuntimeServer` is monkeypatched to a recorder so no real socket is opened.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar, Self

import pytest
from feed_adapter_synthetic import __main__ as main_module
from feed_adapter_synthetic.config import FeedConfig
from feed_adapter_synthetic.sinks import RedisTickSink
from redis.exceptions import ConnectionError as RedisConnectionError


class _FakeRedisClient:
    """A `redis.asyncio.Redis` stand-in: `ping` fails a configurable number of times, then
    succeeds; `aclose` just counts calls."""

    def __init__(self, *, ping_failures: int = 0) -> None:
        self.ping_failures = ping_failures
        self.ping_calls = 0
        self.aclose_calls = 0

    async def ping(self) -> None:
        self.ping_calls += 1
        if self.ping_calls <= self.ping_failures:
            raise RedisConnectionError("redis is not up yet")

    async def aclose(self) -> None:
        self.aclose_calls += 1


class _RecordingRuntimeServer:
    """Stands in for `tqtk_common.RuntimeServer` - records how it was constructed, opens no
    socket."""

    instances: ClassVar[list[_RecordingRuntimeServer]] = []

    def __init__(self, readiness, *, host, port, registry=None) -> None:
        self.readiness = readiness
        self.host = host
        self.port = port
        type(self).instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


@pytest.fixture(autouse=True)
def _reset_recorder():
    _RecordingRuntimeServer.instances = []
    yield


async def _noop_sleep(_seconds: float) -> None:
    pass


# --- wait_for_redis: /ready reflects the Redis connection, per task 5.2's neighbor and 5.7 -------


def test_wait_for_redis_marks_connected_once_ping_succeeds():
    readiness = main_module.Readiness(main_module.REDIS_DEPENDENCY)
    client = _FakeRedisClient(ping_failures=0)
    stop = asyncio.Event()

    assert not readiness.is_ready
    asyncio.run(main_module.wait_for_redis(client.ping, readiness, stop, sleep=_noop_sleep))

    assert readiness.is_ready
    assert client.ping_calls == 1


def test_wait_for_redis_retries_on_connection_errors_until_it_succeeds():
    readiness = main_module.Readiness(main_module.REDIS_DEPENDENCY)
    client = _FakeRedisClient(ping_failures=3)
    stop = asyncio.Event()
    sleep_calls: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    asyncio.run(
        main_module.wait_for_redis(
            client.ping, readiness, stop, retry_seconds=0.01, sleep=recording_sleep
        )
    )

    assert readiness.is_ready
    assert client.ping_calls == 4
    assert sleep_calls == [0.01, 0.01, 0.01]


def test_wait_for_redis_stops_retrying_once_stop_is_set():
    readiness = main_module.Readiness(main_module.REDIS_DEPENDENCY)
    client = _FakeRedisClient(ping_failures=1_000_000)  # never succeeds on its own
    stop = asyncio.Event()

    async def sleep_then_stop(_seconds: float) -> None:
        stop.set()

    asyncio.run(main_module.wait_for_redis(client.ping, readiness, stop, sleep=sleep_then_stop))

    assert not readiness.is_ready


def test_wait_for_redis_calls_on_connected_exactly_once():
    readiness = main_module.Readiness(main_module.REDIS_DEPENDENCY)
    client = _FakeRedisClient()
    stop = asyncio.Event()
    calls = []

    asyncio.run(
        main_module.wait_for_redis(
            client.ping, readiness, stop, sleep=_noop_sleep, on_connected=lambda: calls.append(1)
        )
    )

    assert calls == [1]


# --- _run_service: composition wiring (tasks 5.6, 5.7) -------------------------------------------


def test_run_service_starts_a_runtime_server_bound_to_the_configured_host_and_port(monkeypatch):
    monkeypatch.setattr(main_module, "RuntimeServer", _RecordingRuntimeServer)

    async def fake_run_synthetic_feed(symbols, **kwargs) -> None:
        return None

    monkeypatch.setattr(main_module, "run_synthetic_feed", fake_run_synthetic_feed)

    config = FeedConfig(runtime_host="127.0.0.1", runtime_port=12345, redis_url="redis://unused/0")
    stop = asyncio.Event()
    fake_redis = _FakeRedisClient()

    asyncio.run(
        main_module._run_service(config, stop, symbols=("EURUSD",), redis_client=fake_redis)
    )

    assert len(_RecordingRuntimeServer.instances) == 1
    server = _RecordingRuntimeServer.instances[0]
    assert server.host == "127.0.0.1"
    assert server.port == 12345
    assert server.readiness.snapshot() == {main_module.REDIS_DEPENDENCY: True}


def test_run_service_wires_config_and_symbols_into_run_synthetic_feed(monkeypatch):
    monkeypatch.setattr(main_module, "RuntimeServer", _RecordingRuntimeServer)
    calls: dict = {}

    async def fake_run_synthetic_feed(symbols, **kwargs) -> None:
        calls["symbols"] = symbols
        calls["kwargs"] = kwargs

    monkeypatch.setattr(main_module, "run_synthetic_feed", fake_run_synthetic_feed)

    config = FeedConfig(tick_rate_per_symbol=2.5, redis_url="redis://unused/0")
    stop = asyncio.Event()
    fake_redis = _FakeRedisClient()

    asyncio.run(
        main_module._run_service(
            config, stop, symbols=("EURUSD", "USDJPY"), redis_client=fake_redis
        )
    )

    assert calls["symbols"] == ("EURUSD", "USDJPY")
    assert calls["kwargs"]["pacing_multiplier"] == 2.5
    assert calls["kwargs"]["stop"] is stop
    assert isinstance(calls["kwargs"]["sink"], RedisTickSink)


def test_run_service_falls_back_to_discover_symbols_when_none_are_given(monkeypatch):
    monkeypatch.setattr(main_module, "RuntimeServer", _RecordingRuntimeServer)
    monkeypatch.setattr(main_module, "discover_symbols", lambda: ("EURUSD", "GBPUSD"))
    calls: dict = {}

    async def fake_run_synthetic_feed(symbols, **kwargs) -> None:
        calls["symbols"] = symbols

    monkeypatch.setattr(main_module, "run_synthetic_feed", fake_run_synthetic_feed)

    config = FeedConfig(redis_url="redis://unused/0")
    stop = asyncio.Event()

    asyncio.run(main_module._run_service(config, stop, redis_client=_FakeRedisClient()))

    assert calls["symbols"] == ("EURUSD", "GBPUSD")


def test_run_service_closes_a_redis_client_it_created_itself(monkeypatch):
    monkeypatch.setattr(main_module, "RuntimeServer", _RecordingRuntimeServer)

    async def fake_run_synthetic_feed(symbols, **kwargs) -> None:
        return None

    monkeypatch.setattr(main_module, "run_synthetic_feed", fake_run_synthetic_feed)
    owned_client = _FakeRedisClient()
    monkeypatch.setattr(main_module.Redis, "from_url", staticmethod(lambda _url: owned_client))

    config = FeedConfig(redis_url="redis://unused/0")
    stop = asyncio.Event()

    asyncio.run(main_module._run_service(config, stop, symbols=("EURUSD",)))

    assert owned_client.aclose_calls == 1


def test_run_service_does_not_close_a_redis_client_it_was_given(monkeypatch):
    monkeypatch.setattr(main_module, "RuntimeServer", _RecordingRuntimeServer)

    async def fake_run_synthetic_feed(symbols, **kwargs) -> None:
        return None

    monkeypatch.setattr(main_module, "run_synthetic_feed", fake_run_synthetic_feed)

    config = FeedConfig(redis_url="redis://unused/0")
    stop = asyncio.Event()
    given_client = _FakeRedisClient()

    asyncio.run(
        main_module._run_service(config, stop, symbols=("EURUSD",), redis_client=given_client)
    )

    assert given_client.aclose_calls == 0
