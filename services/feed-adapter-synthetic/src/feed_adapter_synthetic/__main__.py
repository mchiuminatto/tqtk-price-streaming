"""Entrypoint: `python -m feed_adapter_synthetic` runs the adapter as a long-lived service.

Wires `FeedConfig` (12-factor config) to `run_synthetic_feed` and `RedisTickSink`, and serves
`/health`, `/ready`, `/metrics` per the `service-runtime` capability. `/ready` reports two
dependencies rather than reporting ready the instant the process starts, per
`tqtk_common.server.Readiness`'s own contract: `redis` (the connection - see `wait_for_redis`) and
`calibration` (every symbol's calibration loaded from the calibration store). They are separate so
a store problem reads differently from a connection problem; a store that is unseeded or incomplete
stops the process before any tick is published (`CalibrationStore.load` raises).
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable, Sequence

from redis.asyncio import Redis
from redis.exceptions import RedisError

from tqtk_common import Readiness, RuntimeServer, configure_logging

from .calibration_store import CalibrationError, CalibrationStore
from .config import FeedConfig
from .generator import TickSink, run_synthetic_feed
from .sinks import RedisTickSink

__all__ = ["main"]

_log = logging.getLogger(__name__)

REDIS_DEPENDENCY = "redis"
CALIBRATION_DEPENDENCY = "calibration"

_REDIS_RETRY_SECONDS = 1.0


async def wait_for_redis(
    ping: Callable[[], Awaitable[object]],
    readiness: Readiness,
    stop: asyncio.Event,
    *,
    retry_seconds: float = _REDIS_RETRY_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_connected: Callable[[], None] | None = None,
) -> None:
    """Mark `redis` connected once `ping` succeeds; retries on `RedisError` until then or `stop`.

    Crash-looping on a dependency that is merely still starting (a common Compose ordering) would
    defeat the reason `/ready` exists to report this window rather than hide it - see
    `tqtk_common.server.Readiness`'s module docstring.
    """
    while not stop.is_set():
        try:
            await ping()
        except RedisError:
            _log.debug("redis not reachable yet", extra={"dependency": REDIS_DEPENDENCY})
            await sleep(retry_seconds)
            continue
        readiness.mark_connected(REDIS_DEPENDENCY)
        if on_connected is not None:
            on_connected()
        return


async def _run_service(
    config: FeedConfig,
    stop: asyncio.Event,
    *,
    symbols: Sequence[str] | None = None,
    redis_client: Redis | None = None,
) -> None:
    """Serve the runtime endpoints, wait for Redis, load calibration, then generate until `stop`
    is set.

    `symbols` and `redis_client` default to production behavior (every symbol in the calibration
    store, a client built from `config.redis_url`) and are overridable - the same injection style
    `run_synthetic_feed` itself uses - so a caller can scope a run to a subset of the calibrated
    symbols or supply a pooled connection.
    """
    readiness = Readiness(REDIS_DEPENDENCY, CALIBRATION_DEPENDENCY)
    owns_redis_client = redis_client is None
    client = redis_client if redis_client is not None else Redis.from_url(config.redis_url)
    with RuntimeServer(readiness, host=config.runtime_host, port=config.runtime_port):
        try:
            await wait_for_redis(client.ping, readiness, stop)
            if stop.is_set():
                return
            try:
                calibrations = await CalibrationStore(client).load()
            except CalibrationError as exc:
                _log.error("calibration load failed: %s", exc)
                raise
            selected = tuple(calibrations) if symbols is None else tuple(symbols)
            uncalibrated = sorted(set(selected) - calibrations.keys())
            if uncalibrated:
                raise CalibrationError(f"no calibration in the store for {uncalibrated}")
            readiness.mark_connected(CALIBRATION_DEPENDENCY)
            sink: TickSink = RedisTickSink(client)
            await run_synthetic_feed(
                selected,
                pacing_multiplier=config.tick_rate_per_symbol,
                sink=sink,
                calibrations=calibrations,
                stop=stop,
            )
        finally:
            if owns_redis_client:
                await client.aclose()


def _install_shutdown_handler(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT set `stop` instead of raising, so the feed loop exits cleanly."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)


def main() -> None:
    config = FeedConfig()
    configure_logging(config)
    _log.info("starting", extra={"service": config.service_name})

    async def _entrypoint() -> None:
        stop = asyncio.Event()
        _install_shutdown_handler(stop)
        await _run_service(config, stop)

    asyncio.run(_entrypoint())
    _log.info("stopped", extra={"service": config.service_name})


if __name__ == "__main__":
    main()
