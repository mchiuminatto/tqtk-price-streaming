"""Per-instrument price model for the synthetic feed - see `docs/synthetic-price.md`.

Per that doc, generation needs five things per symbol: `p_0`, the minimum price-change unit, and a
return, a spread and a tick-interval distribution (each a fitted family plus parameters).
`SymbolCalibration` carries them; `generator._RandomWalk` consumes one per symbol, and
`calibration_store.CalibrationStore` builds them from the calibration store at startup. The values
are defined only in the calibration seed script, `deploy/calibration/calibration.redis`. Per the
doc's Constraints section, `p_0` and the minimum price-change unit are prices and are represented
as `Decimal`.

This calibration is specific to the synthetic feed - a real provider's adapter (e.g. the future
`dukascopy` one) has no equivalent step, since it publishes prices a venue actually quoted rather
than generating them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .distributions import Distribution

__all__ = ["SymbolCalibration"]


@dataclass(frozen=True, slots=True)
class SymbolCalibration:
    """One symbol's price/timing model - see module docstring."""

    initial_price: Decimal
    price_increment: Decimal
    return_distribution: Distribution
    spread_distribution: Distribution
    interval_distribution: Distribution
