"""Tasks 1.4 and 1.5: unit conversion and the keyspace the seeder writes."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tools.calibration_seed.keyspace import (
    QUANTITIES,
    build_keyspace,
    seed,
    to_stored,
)
from tools.calibration_seed.tables import (
    INSTRUMENTS,
    INTERVAL_FITS,
    RETURN_FITS,
    SPREAD_FITS,
    SYMBOLOGY,
)

from .fake_redis import FakeRedis

_PIP = Decimal("0.00001")

# --- 1.4: code units -> stored units ---------------------------------------------------------


def test_pip_parameter_is_divided_by_pip_size() -> None:
    assert to_stored(4.92226e-05, "pip", _PIP) == "4.92226"


def test_ms_parameter_is_multiplied_by_1000() -> None:
    assert to_stored(0.466108, "ms", _PIP) == "466.108"


def test_quote_parameter_is_unchanged() -> None:
    assert to_stored(1.06012e-05, "quote", _PIP) == "0.0000106012"


def test_dimensionless_parameter_is_not_scaled() -> None:
    assert to_stored(0.422882, "dimensionless", _PIP) == "0.422882"


def test_unknown_unit_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown unit 'furlong'"):
        to_stored(1.0, "furlong", _PIP)


def test_stored_values_are_positional_not_scientific() -> None:
    assert to_stored(-9.41761e-08, "quote", _PIP) == "-0.0000000941761"


def test_eurusd_spread_keys_carry_units_per_parameter() -> None:
    keyspace = build_keyspace()
    prefix = "calib:EUR/USD:spread"
    assert keyspace.strings[f"{prefix}:family"] == "gamma"
    assert keyspace.strings[f"{prefix}:param_count"] == "3"
    assert keyspace.hashes[f"{prefix}:param:0"] == {
        "name": "a",
        "value": "0.422882",
        "unit": "dimensionless",
    }
    assert keyspace.hashes[f"{prefix}:param:1"] == {
        "name": "loc",
        "value": "0.999855",
        "unit": "pip",
    }
    assert keyspace.hashes[f"{prefix}:param:2"] == {
        "name": "scale",
        "value": "4.92226",
        "unit": "pip",
    }


# --- 1.5: the keyspace and the seeder ---------------------------------------------------------


def _expected_keys() -> set[str]:
    keys = {"symbology", "calib:symbols", "calib:quantities"}
    for venue in SYMBOLOGY:
        keys.add(f"instrument:{venue}")
        for quantity, table in (
            ("return", RETURN_FITS),
            ("spread", SPREAD_FITS),
            ("interval", INTERVAL_FITS),
        ):
            prefix = f"calib:{venue}:{quantity}"
            keys |= {f"{prefix}:family", f"{prefix}:param_count"}
            keys |= {f"{prefix}:param:{n}" for n in range(len(table[venue].params))}
    return keys


def test_seed_writes_exactly_the_keyspace() -> None:
    store = FakeRedis()
    seed(store.sync())

    assert store.all_keys() == _expected_keys()
    assert store.hashes["symbology"] == SYMBOLOGY
    assert store.sets["calib:symbols"] == set(SYMBOLOGY)
    assert store.sets["calib:quantities"] == set(QUANTITIES)
    assert store.hashes["instrument:USD/JPY"] == {
        "pip_size": "0.001",
        "quote_currency": "JPY",
        "initial_price": "159.773",
    }


def test_seed_writes_in_one_transaction() -> None:
    store = FakeRedis()
    seed(store.sync())
    assert store.round_trips == 1


def test_second_run_leaves_the_keyspace_identical() -> None:
    store = FakeRedis()
    seed(store.sync())
    first = store.snapshot()
    seed(store.sync())
    assert store.snapshot() == first


def test_dropped_instrument_disappears_from_every_key() -> None:
    store = FakeRedis()
    seed(store.sync())

    kept = {venue: file for venue, file in SYMBOLOGY.items() if venue != "EUR/USD"}
    seed(
        store.sync(),
        build_keyspace(
            kept,
            {venue: INSTRUMENTS[venue] for venue in kept},
            {
                "return": {venue: RETURN_FITS[venue] for venue in kept},
                "spread": {venue: SPREAD_FITS[venue] for venue in kept},
                "interval": {venue: INTERVAL_FITS[venue] for venue in kept},
            },
        ),
    )

    assert "EUR/USD" not in store.sets["calib:symbols"]
    assert "EUR/USD" not in store.hashes["symbology"]
    assert not [key for key in store.all_keys() if "EUR/USD" in key]


def test_seed_leaves_unrelated_keys_alone() -> None:
    store = FakeRedis()
    store.strings["bar_state:synthetic:EURUSD:1m"] = "checkpoint"
    seed(store.sync())
    assert store.strings["bar_state:synthetic:EURUSD:1m"] == "checkpoint"


def test_tables_disagreeing_on_instruments_are_rejected() -> None:
    with pytest.raises(ValueError, match="differently from SYMBOLOGY"):
        build_keyspace(
            SYMBOLOGY,
            {venue: INSTRUMENTS[venue] for venue in SYMBOLOGY if venue != "EUR/USD"},
        )
