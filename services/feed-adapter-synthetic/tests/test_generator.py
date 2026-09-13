"""Verification for tasks 5.1, 5.4, and 5.5: tick generation scoped to exactly the configured
symbol set, and the calibrated price/pacing model from `docs/sythetic-price.md`.

`sleep` is a no-op and `max_ticks_per_symbol` bounds each run, so these assert the generator's own
behavior deterministically rather than depending on wall-clock timing. Structural tests inject a
zero-variance `SymbolCalibration` so they don't depend on random price movement; the calibrated
distribution itself is verified separately, directly against `_RandomWalk`.
"""

from __future__ import annotations

import asyncio
import statistics
from itertools import pairwise

import pytest
from feed_adapter_synthetic.calibration import SymbolCalibration
from feed_adapter_synthetic.generator import TickSink, _RandomWalk, run_synthetic_feed

from tqtk_common.records import Tick

SYMBOLS = ("EURUSD", "USDJPY", "GBPUSD")

# Zero variance: every generated price stays at `initial_price`, every interval at
# `interval_mean` - deterministic, so tests below can assert on tick count/routing/shape without
# depending on the random walk itself (that's `_RandomWalk`'s own test section, further down).
_FIXED_CALIBRATION = SymbolCalibration(
    initial_price=1.10000,
    price_increment=0.00001,
    return_mean=0.0,
    return_stdev=0.0,
    interval_mean=0.01,
    interval_stdev=0.0,
)


class _FakeSink:
    """Collects every published tick in memory - no Redis required to verify this task."""

    def __init__(self) -> None:
        self.published: list[Tick] = []

    async def publish(self, tick: Tick) -> None:
        self.published.append(tick)


async def _noop_sleep(_seconds: float) -> None:
    """Advances the loop without actually waiting - keeps the test fast and deterministic."""


def _run(sink: TickSink, *, symbols=SYMBOLS, max_ticks_per_symbol: int = 4) -> None:
    asyncio.run(
        run_synthetic_feed(
            symbols,
            pacing_multiplier=1.0,
            sink=sink,
            calibrations={symbol: _FIXED_CALIBRATION for symbol in symbols},
            sleep=_noop_sleep,
            max_ticks_per_symbol=max_ticks_per_symbol,
        )
    )


# --- the task's own scenario: exactly the configured symbols, and no others -------------------


def test_publishes_only_for_the_configured_symbols_and_no_others():
    sink = _FakeSink()

    _run(sink)

    published_symbols = {tick.symbol for tick in sink.published}
    assert published_symbols == set(SYMBOLS)


def test_publishes_the_configured_number_of_ticks_per_symbol():
    sink = _FakeSink()

    _run(sink, max_ticks_per_symbol=7)

    counts = {symbol: 0 for symbol in SYMBOLS}
    for tick in sink.published:
        counts[tick.symbol] += 1
    assert counts == {symbol: 7 for symbol in SYMBOLS}


def test_a_single_configured_symbol_publishes_only_that_symbol():
    sink = _FakeSink()

    _run(sink, symbols=("EURUSD",))

    assert {tick.symbol for tick in sink.published} == {"EURUSD"}


def test_rejects_an_empty_symbol_set():
    with pytest.raises(ValueError, match="symbols must be non-empty"):
        _run(_FakeSink(), symbols=())


# --- generated ticks are schema-valid Tick records ----------------------------------------------


def test_every_published_record_is_a_well_formed_tick():
    sink = _FakeSink()

    _run(sink, max_ticks_per_symbol=3)

    assert sink.published  # something was actually published
    for tick in sink.published:
        assert isinstance(tick, Tick)
        assert tick.symbol in SYMBOLS
        assert tick.ask >= tick.bid


# --- provider tagging, recv_ts stamping, and session/seq assignment (task 5.2) ------------------


def test_every_tick_is_tagged_with_the_synthetic_provider():
    sink = _FakeSink()

    _run(sink, max_ticks_per_symbol=2)

    assert sink.published
    assert all(tick.provider == "synthetic" for tick in sink.published)


def test_recv_ts_is_non_decreasing_per_symbol_even_when_the_clock_goes_backwards():
    """The data contract requires `recv_ts` monotonic per (provider, symbol); `_run_symbol` clamps
    to `max(clock(), last_recv_ts)` precisely because a real clock is not guaranteed to always
    advance between calls."""
    sink = _FakeSink()
    raw_clock_values = iter([1_000, 2_000, 1_500, 1_500, 3_000])

    def backwards_clock() -> int:
        return next(raw_clock_values)

    asyncio.run(
        run_synthetic_feed(
            ("EURUSD",),
            pacing_multiplier=1.0,
            sink=sink,
            calibrations={"EURUSD": _FIXED_CALIBRATION},
            clock=backwards_clock,
            sleep=_noop_sleep,
            max_ticks_per_symbol=5,
        )
    )

    recv_timestamps = [tick.recv_ts for tick in sink.published]
    assert recv_timestamps == [1_000, 2_000, 2_000, 2_000, 3_000]
    assert recv_timestamps == sorted(recv_timestamps)


def test_a_restart_produces_a_new_session_id_with_seq_reset_to_zero():
    """Per `tqtk_common.session.FeedSession`'s own docstring, constructing a second `FeedSession`
    *is* what a restart means to a consumer - two separate `run_synthetic_feed` calls (each
    defaulting to a fresh session) simulate two adapter process runs."""
    first_sink = _FakeSink()
    second_sink = _FakeSink()

    for sink in (first_sink, second_sink):
        asyncio.run(
            run_synthetic_feed(
                ("EURUSD",),
                pacing_multiplier=1.0,
                sink=sink,
                calibrations={"EURUSD": _FIXED_CALIBRATION},
                sleep=_noop_sleep,
                max_ticks_per_symbol=3,
            )
        )

    first_session_ids = {tick.session_id for tick in first_sink.published}
    second_session_ids = {tick.session_id for tick in second_sink.published}
    assert len(first_session_ids) == 1
    assert len(second_session_ids) == 1
    assert first_session_ids != second_session_ids

    assert [tick.seq for tick in first_sink.published] == [0, 1, 2]
    assert [tick.seq for tick in second_sink.published] == [0, 1, 2]


# --- calibrated price generation (task 5.4): `_RandomWalk` follows `p_t+1 = p_t + N(mu_I, sigma_I)`
# -------------------------------------------------------------------------------------------------


def test_random_walk_returns_approximate_the_calibrated_normal_distribution():
    calibration = SymbolCalibration(
        initial_price=1.10000,
        price_increment=0.00001,
        return_mean=0.00002,
        return_stdev=0.00010,
        interval_mean=1.0,
        interval_stdev=0.1,
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=1234)
    n = 5000

    mids = []
    for _ in range(n):
        bid, ask = walk.next_quote()
        mids.append((bid + ask) / 2)
    returns = [later - earlier for earlier, later in pairwise(mids)]

    # Standard error of the mean over n samples, times a 3-sigma margin for test stability.
    mean_tolerance = 3 * calibration.return_stdev / (n**0.5)
    assert statistics.fmean(returns) == pytest.approx(calibration.return_mean, abs=mean_tolerance)
    assert statistics.pstdev(returns) == pytest.approx(calibration.return_stdev, rel=0.1)


def test_random_walk_prices_are_rounded_to_the_price_increment():
    calibration = SymbolCalibration(
        initial_price=1.10000,
        price_increment=0.00001,
        return_mean=0.0,
        return_stdev=0.0005,
        interval_mean=1.0,
        interval_stdev=0.0,
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=7)

    for _ in range(200):
        bid, ask = walk.next_quote()
        mid = (bid + ask) / 2
        multiple = mid / calibration.price_increment
        assert multiple == pytest.approx(round(multiple), abs=1e-6)


# --- calibrated tick pacing (task 5.5): interval ~ N(mu_It, sigma_It), scaled by the pacing
# multiplier -----------------------------------------------------------------------------------


def test_random_walk_interval_approximates_the_calibrated_distribution_scaled_by_pacing():
    calibration = SymbolCalibration(
        initial_price=1.0,
        price_increment=0.00001,
        return_mean=0.0,
        return_stdev=0.0,
        interval_mean=0.2,
        interval_stdev=0.05,
    )
    pacing_multiplier = 2.0
    walk = _RandomWalk(calibration, pacing_multiplier=pacing_multiplier, seed=99)
    n = 5000

    intervals = [walk.next_interval() for _ in range(n)]

    expected_mean = calibration.interval_mean / pacing_multiplier
    expected_stdev = calibration.interval_stdev / pacing_multiplier
    assert statistics.fmean(intervals) == pytest.approx(
        expected_mean, abs=3 * expected_stdev / (n**0.5)
    )
    assert statistics.pstdev(intervals) == pytest.approx(expected_stdev, rel=0.15)


def test_random_walk_interval_defaults_to_the_samples_own_cadence_at_multiplier_one():
    calibration = SymbolCalibration(
        initial_price=1.0,
        price_increment=0.00001,
        return_mean=0.0,
        return_stdev=0.0,
        interval_mean=0.5,
        interval_stdev=0.1,
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=11)

    intervals = [walk.next_interval() for _ in range(5000)]

    assert statistics.fmean(intervals) == pytest.approx(calibration.interval_mean, rel=0.05)


def test_random_walk_interval_is_never_negative():
    # Deliberately huge stdev relative to mean, to force some raw gaussian samples negative.
    calibration = SymbolCalibration(
        initial_price=1.0,
        price_increment=0.00001,
        return_mean=0.0,
        return_stdev=0.0,
        interval_mean=0.001,
        interval_stdev=1.0,
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=3)

    intervals = [walk.next_interval() for _ in range(2000)]

    assert min(intervals) >= 0.0
