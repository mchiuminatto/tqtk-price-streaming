"""Verification for tasks 5.1, 5.4, and 5.5: tick generation scoped to exactly the configured
symbol set, and the calibrated price/pacing model from `docs/synthetic-price.md`.

`sleep` is a no-op and `max_ticks_per_symbol` bounds each run, so these assert the generator's own
behavior deterministically rather than depending on wall-clock timing. Structural tests inject a
`"constant"`-family `SymbolCalibration` so they don't depend on random price movement; individual
distribution families are verified for statistical correctness separately, in
`test_distributions.py` - the tests here integration-test `_RandomWalk`'s use of them (the
`Decimal` walk, quantization, and `ask = bid + spread`).
"""

from __future__ import annotations

import asyncio
import statistics
from decimal import Decimal
from itertools import pairwise

import pytest
from feed_adapter_synthetic.calibration import SymbolCalibration
from feed_adapter_synthetic.distributions import Distribution
from feed_adapter_synthetic.generator import TickSink, _RandomWalk, run_synthetic_feed

from tqtk_common.records import Tick

SYMBOLS = ("EURUSD", "USDJPY", "GBPUSD")

# Every draw is a fixed point mass: price stays at `initial_price`, spread stays at 0.0002, every
# interval is 0.01s - deterministic, so tests below can assert on tick count/routing/shape without
# depending on the random walk itself (that's `_RandomWalk`'s own test section, further down).
_FIXED_CALIBRATION = SymbolCalibration(
    initial_price=Decimal("1.10000"),
    price_increment=Decimal("0.00001"),
    return_distribution=Distribution("constant", (0.0,)),
    spread_distribution=Distribution("constant", (0.0002,)),
    interval_distribution=Distribution("constant", (0.01,)),
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


# --- a fixed seed gives each symbol its own sequence, not a shared one -------------------------


def test_a_fixed_seed_gives_each_symbol_an_independent_sequence():
    """A single `seed` passed to `run_synthetic_feed` must not replay identically across symbols -
    see `generator._derive_symbol_seed`. Both symbols share the same calibration here specifically
    so that, before the fix, they would draw byte-for-byte identical bid/ask sequences."""
    calibration = SymbolCalibration(
        initial_price=Decimal("1.10000"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("laplace", (0.0, 0.0005)),
        spread_distribution=Distribution("constant", (0.0002,)),
        interval_distribution=Distribution("constant", (1.0,)),
    )
    symbols = ("EURUSD", "USDJPY")
    sink = _FakeSink()

    asyncio.run(
        run_synthetic_feed(
            symbols,
            pacing_multiplier=1.0,
            sink=sink,
            calibrations=dict.fromkeys(symbols, calibration),
            sleep=_noop_sleep,
            max_ticks_per_symbol=5,
            seed=42,
        )
    )

    by_symbol: dict[str, list[tuple[float, float]]] = {symbol: [] for symbol in symbols}
    for tick in sink.published:
        by_symbol[tick.symbol].append((tick.bid, tick.ask))
    assert by_symbol["EURUSD"] != by_symbol["USDJPY"]


def test_a_fixed_seed_still_reproduces_the_same_sequence_per_symbol_across_runs():
    calibration = SymbolCalibration(
        initial_price=Decimal("1.10000"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("laplace", (0.0, 0.0005)),
        spread_distribution=Distribution("constant", (0.0002,)),
        interval_distribution=Distribution("constant", (1.0,)),
    )

    def _run_once() -> list[tuple[float, float]]:
        sink = _FakeSink()
        asyncio.run(
            run_synthetic_feed(
                ("EURUSD",),
                pacing_multiplier=1.0,
                sink=sink,
                calibrations={"EURUSD": calibration},
                sleep=_noop_sleep,
                max_ticks_per_symbol=5,
                seed=42,
            )
        )
        return [(tick.bid, tick.ask) for tick in sink.published]

    assert _run_once() == _run_once()


# --- calibrated price generation (task 5.4): `_RandomWalk` follows
# `bid_t+1 = bid_t + D_R(params)`, `ask_t+1 = bid_t+1 + D_S(params)` -------------------------------


def test_random_walk_ask_equals_bid_plus_the_drawn_spread():
    """Replaces the old `mid +/- spread/2` model: ask is bid plus a (possibly per-tick random)
    spread, never symmetric around a synthetic midpoint."""
    calibration = SymbolCalibration(
        initial_price=Decimal("1.10000"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("constant", (0.0,)),
        spread_distribution=Distribution("constant", (0.0002,)),
        interval_distribution=Distribution("constant", (1.0,)),
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=1)

    for _ in range(50):
        bid, ask = walk.next_quote()
        assert bid == Decimal("1.10000")  # the return is always exactly 0
        assert ask - bid == Decimal("0.0002")


def test_random_walk_bid_returns_approximate_the_calibrated_distribution():
    loc, scale = 0.00002, 0.00010
    calibration = SymbolCalibration(
        initial_price=Decimal("1.10000"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("laplace", (loc, scale)),
        spread_distribution=Distribution("constant", (0.0002,)),
        interval_distribution=Distribution("constant", (1.0,)),
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=1234)
    n = 5000

    bids = []
    for _ in range(n):
        bid, ask = walk.next_quote()
        assert ask - bid == Decimal("0.0002")
        bids.append(float(bid))
    returns = [later - earlier for earlier, later in pairwise(bids)]

    expected_stdev = scale * (2**0.5)  # Var(Laplace) = 2*b^2
    mean_tolerance = 3 * expected_stdev / (n**0.5)
    assert statistics.fmean(returns) == pytest.approx(loc, abs=mean_tolerance)
    assert statistics.pstdev(returns) == pytest.approx(expected_stdev, rel=0.1)


def test_random_walk_prices_are_rounded_to_the_price_increment():
    calibration = SymbolCalibration(
        initial_price=Decimal("1.10000"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("laplace", (0.0, 0.0005)),
        spread_distribution=Distribution("gamma", (2.0, 0.00001, 0.00005)),
        interval_distribution=Distribution("constant", (1.0,)),
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=7)

    for _ in range(200):
        bid, ask = walk.next_quote()
        assert isinstance(bid, Decimal)
        assert isinstance(ask, Decimal)
        bid_multiple = bid / calibration.price_increment
        ask_multiple = ask / calibration.price_increment
        assert bid_multiple == round(bid_multiple)
        assert ask_multiple == round(ask_multiple)


# --- calibrated tick pacing (task 5.5): interval ~ D_T(params), scaled by the pacing multiplier --


def test_random_walk_interval_approximates_the_calibrated_distribution_scaled_by_pacing():
    mean, stdev = 0.2, 0.05
    calibration = SymbolCalibration(
        initial_price=Decimal("1.0"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("constant", (0.0,)),
        spread_distribution=Distribution("constant", (0.0,)),
        interval_distribution=Distribution("normal", (mean, stdev)),
    )
    pacing_multiplier = 2.0
    walk = _RandomWalk(calibration, pacing_multiplier=pacing_multiplier, seed=99)
    n = 5000

    intervals = [walk.next_interval() for _ in range(n)]

    expected_mean = mean / pacing_multiplier
    expected_stdev = stdev / pacing_multiplier
    assert statistics.fmean(intervals) == pytest.approx(
        expected_mean, abs=3 * expected_stdev / (n**0.5)
    )
    assert statistics.pstdev(intervals) == pytest.approx(expected_stdev, rel=0.15)


def test_random_walk_interval_defaults_to_the_samples_own_cadence_at_multiplier_one():
    mean, stdev = 0.5, 0.1
    calibration = SymbolCalibration(
        initial_price=Decimal("1.0"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("constant", (0.0,)),
        spread_distribution=Distribution("constant", (0.0,)),
        interval_distribution=Distribution("normal", (mean, stdev)),
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=11)

    intervals = [walk.next_interval() for _ in range(5000)]

    assert statistics.fmean(intervals) == pytest.approx(mean, rel=0.05)


def test_random_walk_interval_is_never_negative():
    # Deliberately huge stdev relative to mean, to force some raw draws negative.
    calibration = SymbolCalibration(
        initial_price=Decimal("1.0"),
        price_increment=Decimal("0.00001"),
        return_distribution=Distribution("constant", (0.0,)),
        spread_distribution=Distribution("constant", (0.0,)),
        interval_distribution=Distribution("normal", (0.001, 1.0)),
    )
    walk = _RandomWalk(calibration, pacing_multiplier=1.0, seed=3)

    intervals = [walk.next_interval() for _ in range(2000)]

    assert min(intervals) >= 0.0
