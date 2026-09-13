"""Per-instrument statistical parameters fitted from the sample tick data.

`docs/sythetic-price.md` specifies the synthetic feed's price and tick-timing model as two normal
distributions fitted per instrument from its sample data: `mu_I`/`sigma_I` for returns and
`mu_It`/`sigma_It` for tick intervals. `p_0` and the minimum price-change unit are read from the
same sample. `_RandomWalk` (`generator.py`) consumes a `SymbolCalibration` per symbol; this module
only computes it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import pyarrow.parquet as pq

from .symbols import find_symbol_file

__all__ = ["SymbolCalibration", "compute_calibration"]


@dataclass(frozen=True, slots=True)
class SymbolCalibration:
    """One symbol's price/timing model, fitted from its `data/*.parquet` sample."""

    initial_price: float
    price_increment: float
    return_mean: float
    return_stdev: float
    interval_mean: float
    interval_stdev: float


def _decimal_places(value: float) -> int:
    """How many digits follow the decimal point in `value`'s shortest round-tripping form.

    `repr` (what `str` uses for a `float`) is the shortest decimal string that reads back to the
    same value, so for a price genuinely quoted to N decimals it recovers N exactly - except a
    value with trailing zeros in its true precision (`1.10000`) reprs as `"1.1"`, understating it.
    `compute_calibration` guards against that by taking the max over the whole sample rather than
    reading a single price.
    """
    text = repr(value)
    if "e" in text or "E" in text:
        # Scientific notation would only appear for a price far outside any real instrument's
        # range - fail loudly rather than guess at a decimal count.
        raise ValueError(f"cannot infer a decimal count from {text!r}")
    _, _, decimals = text.partition(".")
    return len(decimals)


def compute_calibration(symbol: str, data_dir: Path | None = None) -> SymbolCalibration:
    """Fit `SymbolCalibration` for `symbol` from its sample file under `data_dir`.

    Uses the sample's `Bid` column (not the `Bid`/`Ask` mid) as the calibrated price series:
    averaging the two introduces sub-pip floating-point noise, since `Bid` and `Ask` tick
    independently at the instrument's real pip grain.
    """
    path = find_symbol_file(symbol, data_dir)
    table = pq.read_table(path, columns=["time_art", "Bid"])
    prices: list[float] = table.column("Bid").to_pylist()
    timestamps = table.column("time_art").to_pylist()
    if len(prices) < 2:
        raise ValueError(f"{path.name!r} has fewer than 2 ticks; cannot fit a distribution")

    returns = [later - earlier for earlier, later in pairwise(prices)]
    intervals = [(later - earlier).total_seconds() for earlier, later in pairwise(timestamps)]

    # Per docs/sythetic-price.md: the minimum change position is the last decimal place present
    # in a sample price - i.e. the instrument's pip grain, read directly off the data rather than
    # inferred from how two ticks happen to differ.
    decimal_places = max(_decimal_places(price) for price in prices)

    return SymbolCalibration(
        initial_price=prices[0],
        price_increment=10**-decimal_places,
        return_mean=statistics.fmean(returns),
        return_stdev=statistics.pstdev(returns),
        interval_mean=statistics.fmean(intervals),
        interval_stdev=statistics.pstdev(intervals),
    )
