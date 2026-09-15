"""Verification for task 5.3: per-symbol calibration assembly.

`initial_price`/`price_increment` are derived from a symbol's real sample data (unchanged
behavior); `return`/`spread`/`interval` distributions come from `distributions.py`'s accessors
over its fixed per-symbol tables rather than being fitted from the sample - see `calibration.py`'s
and `distributions.py`'s module docstrings for why.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from feed_adapter_synthetic import distributions
from feed_adapter_synthetic.calibration import compute_calibration

_BIDS = [1.10000, 1.10001, 1.10003, 1.10002, 1.10002]
_ASKS = [1.10020, 1.10021, 1.10023, 1.10022, 1.10022]
_TIMES_MS = [0, 1000, 2500, 3000, 4200]


_EPOCH = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


def _write_sample(tmp_path: Path, symbol: str = "EURUSD") -> None:
    table = pa.table(
        {
            "time_art": pa.array(
                [_EPOCH + dt.timedelta(milliseconds=ms) for ms in _TIMES_MS],
                type=pa.timestamp("ms"),
            ),
            "Ask": pa.array(_ASKS, type=pa.float64()),
            "Bid": pa.array(_BIDS, type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / f"{symbol}_Ticks_2026.01.01_2026.01.02.parquet")


# --- initial_price / price_increment still come from the sample data ----------------------------


def test_derives_initial_price_and_increment_from_the_fixture_sample(tmp_path: Path):
    _write_sample(tmp_path)

    calibration = compute_calibration("EURUSD", tmp_path)

    assert isinstance(calibration.initial_price, Decimal)
    assert isinstance(calibration.price_increment, Decimal)
    assert float(calibration.initial_price) == pytest.approx(_BIDS[0])
    # Max decimal count among the Bids: 1.10000 -> 1 (trailing zeros drop out of repr), the rest
    # -> 5, so the minimum change position lands on the 5th decimal.
    assert calibration.price_increment == Decimal("1e-5")


def test_derives_from_bid_not_the_bid_ask_mid(tmp_path: Path):
    """A mid-price series would introduce sub-pip float noise (see calibration.py) - Bid alone
    stays on the instrument's real pip grain."""
    _write_sample(tmp_path)

    calibration = compute_calibration("EURUSD", tmp_path)

    assert float(calibration.initial_price) == pytest.approx(_BIDS[0])
    assert float(calibration.initial_price) != pytest.approx((_BIDS[0] + _ASKS[0]) / 2)


# --- return/spread/interval distributions come from distributions.py, not the sample ------------


def test_distributions_are_looked_up_from_distributions_py_not_fitted_from_the_sample(
    tmp_path: Path,
):
    """The fixture's own Bid values would fit very different parameters than the real EURUSD
    sample - if `compute_calibration` were still fitting from data, this would fail."""
    _write_sample(tmp_path)

    calibration = compute_calibration("EURUSD", tmp_path)

    assert calibration.return_distribution == distributions.return_distribution("EURUSD")
    assert calibration.spread_distribution == distributions.spread_distribution("EURUSD")
    assert calibration.interval_distribution == distributions.interval_distribution("EURUSD")


def test_raises_on_a_symbol_with_sample_data_but_no_fitted_distribution(tmp_path: Path):
    """A sample file alone is no longer enough to calibrate a symbol - it also needs an entry in
    distributions.py's tables (see that module's docstring on this deliberate trade-off). The
    message comes from the accessor, so it names the first unfitted quantity and its source doc;
    `test_distributions.py` covers the per-quantity wording."""
    _write_sample(tmp_path, symbol="NOTREGISTERED")

    with pytest.raises(ValueError, match="no fitted return distribution"):
        compute_calibration("NOTREGISTERED", tmp_path)


# --- minimum change position: the last decimal place present, per docs/minimum-change-position.md


def test_derives_the_minimum_change_position_from_the_last_decimal_place(tmp_path: Path):
    """The doc's own examples: 1.1405 -> 4th decimal, 101.23 -> 2nd decimal."""
    cases = [
        ("EURUSD", [1.1400, 1.1405], 1e-04),
        ("USDJPY", [101.20, 101.23], 1e-02),
    ]
    for symbol, prices, expected_increment in cases:
        table = pa.table(
            {
                "time_art": pa.array(
                    [_EPOCH, _EPOCH + dt.timedelta(seconds=1)], type=pa.timestamp("ms")
                ),
                "Ask": pa.array([p + 0.0002 for p in prices], type=pa.float64()),
                "Bid": pa.array(prices, type=pa.float64()),
            }
        )
        pq.write_table(table, tmp_path / f"{symbol}_Ticks_2026.01.01_2026.01.02.parquet")

        calibration = compute_calibration(symbol, tmp_path)

        assert calibration.price_increment == Decimal(str(expected_increment))


def test_minimum_change_position_takes_the_max_decimal_count_over_the_whole_sample(
    tmp_path: Path,
):
    """A price with trailing zeros in its true precision (`1.10000`) reprs with fewer decimals
    than it actually has - taking the max over the sample recovers the true precision regardless
    of which price is checked first."""
    table = pa.table(
        {
            "time_art": pa.array(
                [_EPOCH, _EPOCH + dt.timedelta(seconds=1), _EPOCH + dt.timedelta(seconds=2)],
                type=pa.timestamp("ms"),
            ),
            "Ask": pa.array([1.10020, 1.10020, 1.10041], type=pa.float64()),
            "Bid": pa.array([1.10000, 1.10000, 1.10021], type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / "EURUSD_Ticks_2026.01.01_2026.01.02.parquet")

    calibration = compute_calibration("EURUSD", tmp_path)

    assert calibration.price_increment == Decimal("1e-5")


def test_a_flat_sample_computes_a_valid_calibration(tmp_path: Path):
    """The minimum change position no longer depends on any nonzero return (see calibration.py) -
    a flat sample computes cleanly rather than raising, for a symbol distributions.py knows."""
    table = pa.table(
        {
            "time_art": pa.array(
                [_EPOCH, _EPOCH + dt.timedelta(seconds=1)],
                type=pa.timestamp("ms"),
            ),
            "Ask": pa.array([1.10043, 1.10043], type=pa.float64()),
            "Bid": pa.array([1.10023, 1.10023], type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / "EURUSD_Ticks_2026.01.01_2026.01.02.parquet")

    calibration = compute_calibration("EURUSD", tmp_path)

    assert calibration.price_increment == Decimal("1e-5")
    assert calibration.return_distribution == distributions.return_distribution("EURUSD")


# --- error handling on a degenerate sample -------------------------------------------------------


def test_raises_on_an_empty_sample(tmp_path: Path):
    table = pa.table(
        {
            "time_art": pa.array([], type=pa.timestamp("ms")),
            "Ask": pa.array([], type=pa.float64()),
            "Bid": pa.array([], type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / "EURUSD_Ticks_2026.01.01_2026.01.02.parquet")

    with pytest.raises(ValueError, match="no ticks"):
        compute_calibration("EURUSD", tmp_path)


def test_raises_rather_than_silently_truncating_precision_on_a_non_round_tripping_sample(
    tmp_path: Path,
):
    """A `NaN` (or any price needing more than 8 decimals to round-trip) means the sample is
    corrupt, per `_decimal_places`'s docstring - this must fail calibration, not silently produce
    an 8-decimal `price_increment` for what is really a 5-decimal instrument."""
    table = pa.table(
        {
            "time_art": pa.array(
                [_EPOCH, _EPOCH + dt.timedelta(seconds=1), _EPOCH + dt.timedelta(seconds=2)],
                type=pa.timestamp("ms"),
            ),
            "Ask": pa.array([1.10020, 1.10021, 1.10023], type=pa.float64()),
            "Bid": pa.array([1.10000, float("nan"), 1.10003], type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / "EURUSD_Ticks_2026.01.01_2026.01.02.parquet")

    with pytest.raises(ValueError, match="no price round-trips"):
        compute_calibration("EURUSD", tmp_path)


# --- smoke test against the repo's real sample data ---------------------------------------------


def test_computes_calibration_from_the_repos_real_eurusd_sample():
    calibration = compute_calibration("EURUSD")

    assert calibration.price_increment == Decimal("1e-5")
    assert calibration.return_distribution == distributions.return_distribution("EURUSD")
    assert calibration.spread_distribution == distributions.spread_distribution("EURUSD")
    assert calibration.interval_distribution == distributions.interval_distribution("EURUSD")
