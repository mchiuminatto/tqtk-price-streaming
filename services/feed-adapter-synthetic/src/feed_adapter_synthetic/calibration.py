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


def compute_calibration(symbol: str, data_dir: Path | None = None) -> SymbolCalibration:
    """Fit `SymbolCalibration` for `symbol` from its sample file under `data_dir`.

    Uses the sample's `Bid` column (not the `Bid`/`Ask` mid) as the calibrated price series:
    averaging the two introduces sub-pip floating-point noise into both the return and
    price-increment statistics, since `Bid` and `Ask` tick independently at the instrument's real
    pip grain.
    """
    path = find_symbol_file(symbol, data_dir)
    table = pq.read_table(path, columns=["time_art", "Bid"])
    prices: list[float] = table.column("Bid").to_pylist()
    timestamps = table.column("time_art").to_pylist()
    if len(prices) < 2:
        raise ValueError(f"{path.name!r} has fewer than 2 ticks; cannot fit a distribution")

    returns = [later - earlier for earlier, later in pairwise(prices)]
    intervals = [(later - earlier).total_seconds() for earlier, later in pairwise(timestamps)]

    nonzero_abs_returns = [abs(r) for r in returns if r != 0]
    if not nonzero_abs_returns:
        raise ValueError(f"{path.name!r} has no price movement; cannot derive a price increment")

    return SymbolCalibration(
        initial_price=prices[0],
        # Rounded to absorb the float noise `Bid - Bid` differencing introduces (e.g.
        # 9.999999999843467e-06 for a true 1e-05 pip), while staying well below FX price
        # precision, so it never collapses two genuinely distinct instrument grains together.
        price_increment=round(min(nonzero_abs_returns), 8),
        return_mean=statistics.fmean(returns),
        return_stdev=statistics.pstdev(returns),
        interval_mean=statistics.fmean(intervals),
        interval_stdev=statistics.pstdev(intervals),
    )
