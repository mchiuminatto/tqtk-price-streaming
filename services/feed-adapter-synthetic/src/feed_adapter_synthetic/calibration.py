"""Per-instrument price model, fitted/looked up for the synthetic feed - see `docs/synthetic-price.md`.

Per that doc, generation needs four things per symbol: `p_0` and the minimum price-change unit
(both read directly from the sample data, below), plus a return distribution, a spread
distribution, and a tick-interval distribution (each a fitted family + parameters, looked up from
`distributions.py` - the executable form of `docs/tick-distributions.md`,
`docs/spread-distributions.md`, and `docs/tick-interval-distributions.md`). Per the doc's
Constraints section, `p_0` and the minimum price-change unit are prices and are represented as
`Decimal`. `_RandomWalk` (`generator.py`) consumes a `SymbolCalibration` per symbol; this module
only assembles it.

This calibration is specific to the synthetic feed - a real provider's adapter (e.g. the future
`dukascopy` one) has no equivalent step, since it publishes prices a venue actually quoted rather
than generating them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from .distributions import (
    INTERVAL_DISTRIBUTIONS,
    RETURN_DISTRIBUTIONS,
    SPREAD_DISTRIBUTIONS,
    Distribution,
)
from .symbols import find_symbol_file

__all__ = ["SymbolCalibration", "compute_calibration"]

# FX prices are never quoted anywhere near this many decimals; a search that reached it without
# `prices` round-tripping indicates corrupt sample data, not an instrument this model doesn't
# support yet.
_MAX_DECIMAL_PLACES = 8


@dataclass(frozen=True, slots=True)
class SymbolCalibration:
    """One symbol's price/timing model - see module docstring."""

    initial_price: Decimal
    price_increment: Decimal
    return_distribution: Distribution
    spread_distribution: Distribution
    interval_distribution: Distribution


def _decimal_places(prices: pa.ChunkedArray, *, symbol: str, source: Path) -> int:
    """The fewest decimal places `prices` round-trips through unchanged - the instrument's pip
    grain, per `docs/minimum-change-position.md` (a sample quoted to `1.1405` puts it at the 4th
    decimal, `101.23` at the 2nd).

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
    # No decimal count up to _MAX_DECIMAL_PLACES round-trips every price - FX is never quoted this
    # finely, so this means the sample is corrupt (e.g. a NaN/inf price) rather than an instrument
    # needing more precision. Raising here (instead of silently guessing _MAX_DECIMAL_PLACES) keeps
    # a bad sample from quietly producing a wrong price_increment, per this package's "raise rather
    # than guess" convention - see symbols.find_data_dir/find_symbol_file.
    raise ValueError(
        f"{source.name!r} ({symbol!r}): no price round-trips within {_MAX_DECIMAL_PLACES} decimal "
        "places; the Bid column likely contains a non-finite or corrupt value"
    )


def compute_calibration(symbol: str, data_dir: Path | None = None) -> SymbolCalibration:
    """Assemble `SymbolCalibration` for `symbol`.

    `initial_price` and `price_increment` are read from `symbol`'s sample file under `data_dir`
    (its `Bid` column, not the `Bid`/`Ask` mid: averaging the two introduces sub-pip
    floating-point noise, since `Bid` and `Ask` tick independently at the instrument's real pip
    grain). The return/spread/interval distributions are not re-derived from that sample on every
    call - they're looked up from `distributions.py`'s fixed per-symbol tables (see that module's
    docstring for why).
    """
    path = find_symbol_file(symbol, data_dir)
    table = pq.read_table(path, columns=["Bid"])
    prices = table.column("Bid")
    if len(prices) < 1:
        raise ValueError(f"{path.name!r} has no ticks; cannot calibrate")

    try:
        return_distribution = RETURN_DISTRIBUTIONS[symbol]
        spread_distribution = SPREAD_DISTRIBUTIONS[symbol]
        interval_distribution = INTERVAL_DISTRIBUTIONS[symbol]
    except KeyError as exc:
        raise ValueError(
            f"no fitted return/spread/interval distribution for symbol {symbol!r} in "
            "distributions.py - add it per docs/tick-distributions.md, "
            "docs/spread-distributions.md, and docs/tick-interval-distributions.md"
        ) from exc

    return SymbolCalibration(
        initial_price=Decimal(str(prices[0].as_py())),
        price_increment=Decimal(1).scaleb(-_decimal_places(prices, symbol=symbol, source=path)),
        return_distribution=return_distribution,
        spread_distribution=spread_distribution,
        interval_distribution=interval_distribution,
    )
