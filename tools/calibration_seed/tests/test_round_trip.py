"""Task 2.3: seed -> load reproduces the tool's tables exactly, and only `pip`/`ms` are scaled.

Seeds through the tool into the in-memory fake and loads back through the service's
`CalibrationStore`. The seeder's runtime code never imports the service; only this test does.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from feed_adapter_synthetic.calibration import SymbolCalibration
from feed_adapter_synthetic.calibration_store import CalibrationStore
from feed_adapter_synthetic.distributions import Distribution

from tools.calibration_seed.keyspace import build_keyspace, seed
from tools.calibration_seed.tables import (
    INSTRUMENTS,
    INTERVAL_FITS,
    RETURN_FITS,
    SPREAD_FITS,
    SYMBOLOGY,
    Fit,
    Instrument,
)

from .fake_redis import FakeRedis


def _expected() -> dict[str, SymbolCalibration]:
    def distribution(fit: Fit) -> Distribution:
        return Distribution(fit.family, fit.params)

    return {
        SYMBOLOGY[venue]: SymbolCalibration(
            initial_price=instrument.initial_price,
            price_increment=instrument.pip_size,
            return_distribution=distribution(RETURN_FITS[venue]),
            spread_distribution=distribution(SPREAD_FITS[venue]),
            interval_distribution=distribution(INTERVAL_FITS[venue]),
        )
        for venue, instrument in INSTRUMENTS.items()
    }


def _seed_and_load(store: FakeRedis) -> dict[str, SymbolCalibration]:
    return asyncio.run(CalibrationStore(store.aio()).load())


def test_seed_then_load_reproduces_every_table_value_exactly() -> None:
    store = FakeRedis()
    seed(store.sync())

    loaded = _seed_and_load(store)

    assert list(loaded) == sorted(SYMBOLOGY.values())
    # Exact `==` on frozen dataclasses of floats and Decimals - no tolerance.
    assert loaded == _expected()


def test_only_pip_and_ms_location_scale_are_scaled() -> None:
    # Every parameter is 2.0 in code units, so any wrongly applied scaling shows up as a value
    # other than 2.0 - in the store for the write side, after loading for the read side.
    two = Fit("weibull_min", (2.0, 2.0, 2.0))
    store = FakeRedis()
    seed(
        store.sync(),
        build_keyspace(
            {"T/USD": "TUSD"},
            {"T/USD": Instrument(Decimal("0.001"), "USD", Decimal("1.000"))},
            {"return": {"T/USD": two}, "spread": {"T/USD": two}, "interval": {"T/USD": two}},
        ),
    )

    stored = {
        (quantity, store.hashes[f"calib:T/USD:{quantity}:param:{n}"]["value"])
        for quantity in ("return", "spread", "interval")
        for n in range(3)
    }
    assert stored == {
        ("return", "2"),  # shape, loc and scale all unscaled: quote units
        ("spread", "2"),  # the dimensionless shape
        ("spread", "2000"),  # loc and scale in pips of 0.001
        ("interval", "2"),  # the dimensionless shape
        ("interval", "2000"),  # loc and scale in milliseconds
    }

    calibration = _seed_and_load(store)["TUSD"]
    for distribution in (
        calibration.return_distribution,
        calibration.spread_distribution,
        calibration.interval_distribution,
    ):
        assert distribution.params == (2.0, 2.0, 2.0)
