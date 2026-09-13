"""Verification for task 5.3: per-symbol parameter derivation from sample data."""

from __future__ import annotations

import datetime as dt
import statistics
from itertools import pairwise
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
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


# --- derivation matches a fixture sample's known statistics -------------------------------------


def test_derives_parameters_matching_a_fixture_samples_known_statistics(tmp_path: Path):
    _write_sample(tmp_path)

    calibration = compute_calibration("EURUSD", tmp_path)

    expected_returns = [later - earlier for earlier, later in pairwise(_BIDS)]
    expected_intervals = [(later - earlier) / 1000 for earlier, later in pairwise(_TIMES_MS)]

    assert calibration.initial_price == pytest.approx(_BIDS[0])
    # Smallest nonzero |diff| among [1e-05, 2e-05, -1e-05, 0.0] is 1e-05.
    assert calibration.price_increment == pytest.approx(1e-05)
    assert calibration.return_mean == pytest.approx(statistics.fmean(expected_returns))
    assert calibration.return_stdev == pytest.approx(statistics.pstdev(expected_returns))
    assert calibration.interval_mean == pytest.approx(statistics.fmean(expected_intervals))
    assert calibration.interval_stdev == pytest.approx(statistics.pstdev(expected_intervals))


def test_derives_from_bid_not_the_bid_ask_mid(tmp_path: Path):
    """A mid-price series would introduce sub-pip float noise (see calibration.py) - Bid alone
    stays on the instrument's real pip grain."""
    _write_sample(tmp_path)

    calibration = compute_calibration("EURUSD", tmp_path)

    assert calibration.initial_price == pytest.approx(_BIDS[0])
    assert calibration.initial_price != pytest.approx((_BIDS[0] + _ASKS[0]) / 2)


# --- error handling on a degenerate sample ------------------------------------------------------


def test_raises_on_a_sample_with_no_price_movement(tmp_path: Path):
    table = pa.table(
        {
            "time_art": pa.array(
                [_EPOCH, _EPOCH + dt.timedelta(seconds=1)],
                type=pa.timestamp("ms"),
            ),
            "Ask": pa.array([1.1002, 1.1002], type=pa.float64()),
            "Bid": pa.array([1.1000, 1.1000], type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / "FLAT_Ticks_2026.01.01_2026.01.02.parquet")

    with pytest.raises(ValueError, match="no price movement"):
        compute_calibration("FLAT", tmp_path)


def test_raises_on_a_sample_with_fewer_than_two_ticks(tmp_path: Path):
    table = pa.table(
        {
            "time_art": pa.array([_EPOCH], type=pa.timestamp("ms")),
            "Ask": pa.array([1.1002], type=pa.float64()),
            "Bid": pa.array([1.1000], type=pa.float64()),
        }
    )
    pq.write_table(table, tmp_path / "ONE_Ticks_2026.01.01_2026.01.02.parquet")

    with pytest.raises(ValueError, match="fewer than 2 ticks"):
        compute_calibration("ONE", tmp_path)


# --- smoke test against the repo's real sample data ---------------------------------------------


def test_computes_calibration_from_the_repos_real_eurusd_sample():
    calibration = compute_calibration("EURUSD")

    assert calibration.price_increment == pytest.approx(1e-05)
    assert calibration.interval_mean > 0
    assert calibration.return_stdev > 0
