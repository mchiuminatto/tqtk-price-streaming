"""Statistically-calibrated synthetic tick generation for the configured symbol set.

One `asyncio` task per symbol runs concurrently and independently, each publishing at its own
pace. Generation is decoupled from transport through `TickSink`: `RedisTickSink` (in `sinks.py`)
is the production implementation, and a test supplies an in-memory fake instead of requiring a
live Redis to verify which symbols were published.

Per `docs/synthetic-price.md`, `_RandomWalk` follows a biased random walk calibrated per
instrument: the next bid is `bid_t+1 = bid_t + r_t+1`, `r_t+1` drawn from that symbol's fitted
return distribution (`docs/tick-distributions.md`) and rounded to the instrument's minimum
price-change unit; the ask is `bid_t+1 + spread_t+1`, `spread_t+1` drawn from that symbol's fitted
spread distribution (`docs/spread-distributions.md`) - not the fixed constant this used before
per-symbol spread fitting existed. The wait before the next tick is drawn from that symbol's
fitted tick-interval distribution (`docs/tick-interval-distributions.md`), scaled by the
configured pacing multiplier. `calibration.py` assembles the `SymbolCalibration` these
distributions live on; `distributions.py` implements sampling from each fitted family.

Per that doc's Constraints section, every price value and every value derived directly from a
price - the running bid, the sampled return and spread once drawn, the rounded/published bid and
ask - is a `Decimal`, never a `float`, to keep the walk from accumulating sub-pip floating-point
noise over a long-running process. A distribution's own parameters stay `float`: `Distribution.sample`
is built on `random.Random`, which is float-only, so a sampled return/spread is converted to
`Decimal` immediately after being drawn. The `Tick` wire contract's `Price` type is `float` (a
system-wide contract, not specific to this feed), so the final bid/ask are cast to `float` only at
that publication boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from pathlib import Path
from typing import Protocol

from tqtk_common.records import Tick
from tqtk_common.session import FeedSession

from .calibration import SymbolCalibration, compute_calibration

__all__ = ["TickSink", "run_synthetic_feed"]

PROVIDER = "synthetic"


class TickSink(Protocol):
    """Where a generated tick goes. Implemented by `RedisTickSink` in production, a fake in tests."""

    async def publish(self, tick: Tick) -> None: ...


def _default_clock() -> int:
    """Epoch microseconds, matching the `Tick` contract's `EpochMicros` fields."""
    return time.time_ns() // 1_000


def _derive_symbol_seed(seed: int | None, symbol: str) -> int | None:
    """Mix `seed` with `symbol` so a single `seed` given to `run_synthetic_feed` gives each
    concurrently-run symbol its own reproducible-but-independent draw sequence, instead of every
    symbol's `_RandomWalk` replaying the identical sequence (they'd otherwise move in lockstep,
    or identically when their calibrations also match).

    Built on `hashlib` rather than the builtin `hash()`: string hashing is randomized per-process
    by default (`PYTHONHASHSEED`), which would make the same `seed` produce different per-symbol
    sequences across runs - defeating the reproducibility `seed` exists to provide.
    """
    if seed is None:
        return None
    digest = hashlib.sha256(f"{seed}:{symbol}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


class _RandomWalk:
    """One symbol's calibrated bid/ask random walk and tick pacing - see module docstring."""

    __slots__ = ("_bid", "_calibration", "_pacing_multiplier", "_rng")

    def __init__(
        self,
        calibration: SymbolCalibration,
        *,
        pacing_multiplier: float,
        seed: int | None = None,
    ) -> None:
        self._calibration = calibration
        self._rng = random.Random(seed)
        self._bid = calibration.initial_price
        self._pacing_multiplier = pacing_multiplier

    def next_quote(self) -> tuple[Decimal, Decimal]:
        calibration = self._calibration
        increment = calibration.price_increment
        # `Distribution.sample` is float-only; the draw is converted to `Decimal` immediately,
        # before it touches the `Decimal` price state (see module docstring).
        drawn_return = calibration.return_distribution.sample(self._rng)
        self._bid += Decimal(str(drawn_return))
        # Quantizing to the increment's own exponent is an exact rounding to the nearest multiple
        # of it, since `price_increment` is always a power of ten.
        self._bid = self._bid.quantize(increment, rounding=ROUND_HALF_EVEN)
        # Keeps bid strictly positive, which `Tick`'s `Price` type requires: the smallest
        # representable positive price at this instrument's own grain.
        self._bid = max(self._bid, increment)

        drawn_spread = calibration.spread_distribution.sample(self._rng)
        spread = Decimal(str(drawn_spread)).quantize(increment, rounding=ROUND_HALF_EVEN)
        # A quantized draw can floor to exactly zero at this instrument's grain - a zero spread is
        # a valid quote (ask == bid), a negative one never is.
        spread = max(spread, Decimal(0))
        return self._bid, self._bid + spread

    def next_interval(self) -> float:
        calibration = self._calibration
        delta = calibration.interval_distribution.sample(self._rng)
        return max(delta, 0.0) / self._pacing_multiplier


async def _compute_calibrations(
    symbols: Sequence[str], data_dir: Path | None
) -> dict[str, SymbolCalibration]:
    """Fit every symbol's calibration concurrently, off the event loop.

    `compute_calibration` is synchronous CPU/IO-bound work - reading and vectorizing up to a
    ~1M-row parquet file per symbol. Awaiting it 13 times in a row on the event loop would add
    every symbol's cost to the others' serially, and stall everything else on the loop - including
    shutdown signal handling - for the whole window. `asyncio.to_thread` moves each call to a
    worker thread; `gather` runs them concurrently instead of one after another.
    """
    results = await asyncio.gather(
        *(asyncio.to_thread(compute_calibration, symbol, data_dir) for symbol in symbols)
    )
    return dict(zip(symbols, results, strict=True))


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
    walk = _RandomWalk(
        calibration, pacing_multiplier=pacing_multiplier, seed=_derive_symbol_seed(seed, symbol)
    )
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
            # `Tick.bid`/`ask` are `Price` (`float`) - the system-wide wire contract, unrelated to
            # this feed's internal `Decimal` price arithmetic (see module docstring).
            bid=float(bid),
            ask=float(ask),
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

    `seed`, when given, is not reused verbatim across symbols - each symbol's `_RandomWalk` is
    seeded from `seed` mixed with that symbol's name (see `_derive_symbol_seed`), so a single fixed
    `seed` still gives every symbol its own reproducible-but-independent sequence rather than every
    symbol replaying the same one. `None` (the default, and what the production entrypoint always
    passes) seeds each symbol independently from OS entropy instead.
    """
    if not symbols:
        raise ValueError("symbols must be non-empty: the adapter has nothing to generate")
    resolved_calibrations = (
        calibrations if calibrations is not None else await _compute_calibrations(symbols, data_dir)
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
