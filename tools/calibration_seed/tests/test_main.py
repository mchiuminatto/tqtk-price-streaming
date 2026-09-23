"""Task 1.6: `python -m tools.calibration_seed` exits 0 on success, 1 when Redis is unreachable."""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from tools.calibration_seed.__main__ import DEFAULT_REDIS_URL, main

from .fake_redis import FakeRedis


def test_seeds_and_exits_zero() -> None:
    store = FakeRedis()
    urls: list[str] = []

    def connect(url: str):
        urls.append(url)
        return store.sync()

    assert main(environ={"TQTK_REDIS_URL": "redis://redis:6379/0"}, connect=connect) == 0
    assert urls == ["redis://redis:6379/0"]
    assert "calib:symbols" in store.sets


def test_defaults_to_the_local_redis_url() -> None:
    urls: list[str] = []

    def connect(url: str):
        urls.append(url)
        return FakeRedis().sync()

    main(environ={}, connect=connect)
    assert urls == [DEFAULT_REDIS_URL]


def test_unreachable_redis_exits_one_with_a_message(capsys: pytest.CaptureFixture[str]) -> None:
    class _Unreachable:
        def scan_iter(self, match: str):
            raise RedisConnectionError("Connection refused")

    assert main(environ={}, connect=lambda url: _Unreachable()) == 1
    assert "cannot write to Redis at redis://localhost:6379/0" in capsys.readouterr().err
