"""Per-instrument statistical parameters fitted from the sample tick data.

`docs/sythetic-price.md` specifies the synthetic feed's price and tick-timing model as two normal
distributions fitted per instrument from its sample data: `mu_I`/`sigma_I` for returns and
`mu_It`/`sigma_It` for tick intervals. `p_0` and the minimum price-change unit are read from the
same sample. `_RandomWalk` (`generator.py`) consumes a `SymbolCalibration` per symbol; this module
only computes it.

This calibration is specific to the synthetic feed - a real provider's adapter (e.g. the future
`dukascopy` one) has no equivalent step, since it publishes prices a venue actually quoted rather
than generating them.

Every statistic here runs through `pyarrow.compute` rather than a Python-level loop: a sample can
run to ~1M rows, and a handful of vectorized passes over the whole column (in Arrow's own C++
kernels) is what keeps fitting all 13 configured symbols at adapter startup from being the
multi-second-per-symbol cost a pure-Python `statistics.fmean`/`pstdev` loop over that many rows
otherwise is.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .symbols import find_symbol_file

__all__ = ["SymbolCalibration", "compute_calibration"]

# FX prices are never quoted anywhere near this many decimals; a search that reached it without
# `prices` round-tripping indicates corrupt sample data, not an instrument this model doesn't
# support yet.
_MAX_DECIMAL_PLACES = 8

_MILLIS_PER_SECOND = 1_000.0


@dataclass(frozen=True, slots=True)
class SymbolCalibration:
    """One symbol's price/timing model, fitted from its `data/*.parquet` sample."""

    initial_price: float
    price_increment: float
    return_mean: float
    return_stdev: float
    interval_mean: float
    interval_stdev: float


def _decimal_places(prices: pa.ChunkedArray) -> int:
    """The fewest decimal places `prices` round-trips through unchanged - the instrument's pip
    grain, per `docs/sythetic-price.md` (a sample quoted to `1.1405` puts it at the 4th decimal,
    `101.23` at the 2nd).

    Rounding to fewer decimals than a price's true precision changes it; rounding to at least that
    many is a no-op. So the smallest `decimals` where every price survives `pc.round` unchanged
    finds the sample's precision in one vectorized pass per candidate, over the whole column at
    once - equivalent to, but far cheaper than, formatting every price and taking the max digit
    count. It's naturally robust to a price with trailing zeros in its true precision (`1.10000`):
    such a price survives rounding at any `decimals`, so it never lowers the answer another price
    forces.
    """
    for decimals in range(_MAX_DECIMAL_PLACES + 1):
        rounded = pc.round(prices, ndigits=decimals)
        if pc.all(pc.equal(rounded, prices)).as_py():
            return decimals
    return _MAX_DECIMAL_PLACES


def compute_calibration(symbol: str, data_dir: Path | None = None) -> SymbolCalibration:
    """Fit `SymbolCalibration` for `symbol` from its sample file under `data_dir`.

    Uses the sample's `Bid` column (not the `Bid`/`Ask` mid) as the calibrated price series:
    averaging the two introduces sub-pip floating-point noise, since `Bid` and `Ask` tick
    independently at the instrument's real pip grain.
    """
    path = find_symbol_file(symbol, data_dir)
    table = pq.read_table(path, columns=["time_art", "Bid"])
    prices = table.column("Bid")
    timestamps = table.column("time_art")
    n = len(prices)
    if n < 2:
        raise ValueError(f"{path.name!r} has fewer than 2 ticks; cannot fit a distribution")

    earlier_prices, later_prices = prices.slice(0, n - 1), prices.slice(1, n - 1)
    returns = pc.subtract(later_prices, earlier_prices)

    earlier_ts, later_ts = timestamps.slice(0, n - 1), timestamps.slice(1, n - 1)
    interval_ms = pc.cast(pc.subtract(later_ts, earlier_ts), pa.int64())
    intervals = pc.divide(pc.cast(interval_ms, pa.float64()), _MILLIS_PER_SECOND)

    return SymbolCalibration(
        initial_price=prices[0].as_py(),
        price_increment=10 ** -_decimal_places(prices),
        return_mean=pc.mean(returns).as_py(),
        return_stdev=pc.stddev(returns, ddof=0).as_py(),
        interval_mean=pc.mean(intervals).as_py(),
        interval_stdev=pc.stddev(intervals, ddof=0).as_py(),
    )
