"""Statistically-calibrated synthetic tick generation for the configured symbol set.

One `asyncio` task per symbol runs concurrently and independently, each publishing at its own
pace. Generation is decoupled from transport through `TickSink`: `RedisTickSink` (in `sinks.py`)
is the production implementation, and a test supplies an in-memory fake instead of requiring a
live Redis to verify which symbols were published.

Per `docs/sythetic-price.md`, `_RandomWalk` follows a biased random walk calibrated per instrument
from its `data/*.parquet` sample (`calibration.py`): the next price is `p_t+1 = p_t + r_t+1`,
`r_t+1 ~ N(mu_I, sigma_I)`, rounded to the instrument's minimum price-change unit; the wait before
the next tick is drawn from `N(mu_It, sigma_It)`, scaled by the configured pacing multiplier.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Protocol

from tqtk_common.records import Tick
from tqtk_common.session import FeedSession

from .calibration import SymbolCalibration, compute_calibration

__all__ = ["TickSink", "run_synthetic_feed"]

PROVIDER = "synthetic"

_DEFAULT_SPREAD: float = 0.0002


class TickSink(Protocol):
    """Where a generated tick goes. Implemented by `RedisTickSink` in production, a fake in tests."""

    async def publish(self, tick: Tick) -> None: ...


def _default_clock() -> int:
    """Epoch microseconds, matching the `Tick` contract's `EpochMicros` fields."""
    return time.time_ns() // 1_000


class _RandomWalk:
    """One symbol's calibrated bid/ask random walk and tick pacing - see module docstring."""

    __slots__ = ("_calibration", "_mid", "_pacing_multiplier", "_rng", "_spread")

    def __init__(
        self,
        calibration: SymbolCalibration,
        *,
        pacing_multiplier: float,
        seed: int | None = None,
    ) -> None:
        self._calibration = calibration
        self._rng = random.Random(seed)
        self._mid = calibration.initial_price
        self._spread = _DEFAULT_SPREAD
        self._pacing_multiplier = pacing_multiplier

    def next_quote(self) -> tuple[float, float]:
        calibration = self._calibration
        self._mid += self._rng.gauss(calibration.return_mean, calibration.return_stdev)
        increment = calibration.price_increment
        self._mid = round(round(self._mid / increment) * increment, 10)
        # Keeps `bid = mid - spread/2` positive, which `Tick`'s `Price` type requires.
        self._mid = max(self._mid, self._spread)
        half = self._spread / 2
        return self._mid - half, self._mid + half

    def next_interval(self) -> float:
        calibration = self._calibration
        delta = self._rng.gauss(calibration.interval_mean, calibration.interval_stdev)
        return max(delta, 0.0) / self._pacing_multiplier


async def _run_symbol(
    symbol: str,
    *,
    calibration: SymbolCalibration,
    pacing_multiplier: float,
    seed: int | None,
    session: FeedSession,
    sink: TickSink,
    clock: Callable[[], int],
    sleep: Callable[[float], Awaitable[None]],
    stop: asyncio.Event,
    max_ticks: int | None,
) -> None:
    walk = _RandomWalk(calibration, pacing_multiplier=pacing_multiplier, seed=seed)
    last_recv_ts = 0
    published = 0
    while not stop.is_set() and (max_ticks is None or published < max_ticks):
        bid, ask = walk.next_quote()
        # Monotonic per (provider, symbol): the data contract requires non-decreasing `recv_ts`,
        # and back-to-back calls to the same clock tick can otherwise return an equal or, on some
        # clocks, a non-monotonic value.
        recv_ts = max(clock(), last_recv_ts)
        last_recv_ts = recv_ts
        tick = Tick(
            provider=PROVIDER,
            symbol=symbol,
            provider_ts=recv_ts,  # the synthetic generator is its own "provider"
            recv_ts=recv_ts,
            bid=bid,
            ask=ask,
            session_id=session.session_id,
            seq=session.next_seq(PROVIDER, symbol),
        )
        await sink.publish(tick)
        published += 1
        if max_ticks is None or published < max_ticks:
            await sleep(walk.next_interval())


async def run_synthetic_feed(
    symbols: Sequence[str],
    *,
    pacing_multiplier: float,
    sink: TickSink,
    session: FeedSession | None = None,
    calibrations: Mapping[str, SymbolCalibration] | None = None,
    data_dir: Path | None = None,
    seed: int | None = None,
    clock: Callable[[], int] = _default_clock,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    stop: asyncio.Event | None = None,
    max_ticks_per_symbol: int | None = None,
) -> None:
    """Run one generation task per symbol concurrently, publishing to `sink`.

    Runs until `stop` is set or, if given, each symbol has published `max_ticks_per_symbol` ticks -
    the latter exists so a test can bound a run without depending on wall-clock timing. With
    neither, this runs forever, which is the production shape: the adapter's entrypoint sets
    `stop` from a shutdown signal.

    `calibrations` lets a caller (a test, or an entrypoint with its own caching) supply
    pre-computed `SymbolCalibration`s; when omitted, one is computed per symbol from `data_dir`
    (default: the repository's `data/` directory - see `calibration.compute_calibration`).
    """
    if not symbols:
        raise ValueError("symbols must be non-empty: the adapter has nothing to generate")
    resolved_calibrations = (
        calibrations
        if calibrations is not None
        else {symbol: compute_calibration(symbol, data_dir) for symbol in symbols}
    )
    session = session or FeedSession()
    stop = stop or asyncio.Event()
    await asyncio.gather(
        *(
            _run_symbol(
                symbol,
                calibration=resolved_calibrations[symbol],
                pacing_multiplier=pacing_multiplier,
                seed=seed,
                session=session,
                sink=sink,
                clock=clock,
                sleep=sleep,
                stop=stop,
                max_ticks=max_ticks_per_symbol,
            )
            for symbol in symbols
        )
    )
