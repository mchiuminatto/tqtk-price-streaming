"""Verification for task 5.1: the symbol set comes from `data/*.parquet`, not a hardcoded list."""

from __future__ import annotations

from pathlib import Path

import pytest
from feed_adapter_synthetic.symbols import discover_symbols, find_data_dir, find_symbol_file

EXPECTED_SYMBOLS = (
    "AAPLUSUSD",
    "ARKQUSUSD",
    "AUDJPY",
    "AUDUSD",
    "EURGBP",
    "EURJPY",
    "EURUSD",
    "GBPJPY",
    "GBPUSD",
    "NZDJPY",
    "NZDUSD",
    "USA500IDXUSD",
    "USATECHIDXUSD",
    "USDCAD",
    "USDCHF",
    "USDCNH",
    "USDJPY",
)


def test_discovers_exactly_the_symbols_present_in_the_repos_data_dir():
    assert discover_symbols() == EXPECTED_SYMBOLS


def test_discovers_one_symbol_per_distinct_prefix(tmp_path: Path):
    (tmp_path / "EURUSD_Ticks_2026.01.01_2026.01.02.parquet").touch()
    (tmp_path / "EURUSD_Ticks_2026.01.02_2026.01.03.parquet").touch()  # same symbol, second file
    (tmp_path / "USDJPY_Ticks_2026.01.01_2026.01.02.parquet").touch()

    assert discover_symbols(tmp_path) == ("EURUSD", "USDJPY")


def test_raises_on_an_empty_directory(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        discover_symbols(tmp_path)


def test_raises_on_a_filename_with_no_symbol_prefix(tmp_path: Path):
    (tmp_path / "_no_symbol_prefix.parquet").touch()

    with pytest.raises(ValueError, match="does not start with a symbol prefix"):
        discover_symbols(tmp_path)


def test_find_data_dir_raises_above_a_tree_with_no_data_directory(tmp_path: Path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    with pytest.raises(FileNotFoundError):
        find_data_dir(nested)


def test_find_symbol_file_returns_the_matching_sample(tmp_path: Path):
    (tmp_path / "EURUSD_Ticks_2026.01.01_2026.01.02.parquet").touch()
    (tmp_path / "USDJPY_Ticks_2026.01.01_2026.01.02.parquet").touch()

    found = find_symbol_file("EURUSD", tmp_path)

    assert found.name == "EURUSD_Ticks_2026.01.01_2026.01.02.parquet"


def test_find_symbol_file_raises_when_no_sample_matches(tmp_path: Path):
    (tmp_path / "USDJPY_Ticks_2026.01.01_2026.01.02.parquet").touch()

    with pytest.raises(FileNotFoundError, match="no sample file for symbol 'EURUSD'"):
        find_symbol_file("EURUSD", tmp_path)
