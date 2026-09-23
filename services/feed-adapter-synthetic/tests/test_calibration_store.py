"""Verification for tasks 2.1 and 2.2: `CalibrationStore` loads in three round trips, converts
stored units by `unit` alone, and refuses an incomplete store naming the symbol and key.

The keyspace here is built inline rather than by the seeding tool, so these stay tests of the read
side; the seed -> load round trip over the real tables lives in the tool's own suite.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any, Self

import pytest
from feed_adapter_synthetic.calibration_store import CalibrationError, CalibrationStore
from feed_adapter_synthetic.distributions import Distribution


class _FakePipeline:
    def __init__(self, client: _FakeRedis) -> None:
        self._client = client
        self._ops: list[tuple[str, str]] = []

    def smembers(self, name: str) -> Self:
        self._ops.append(("smembers", name))
        return self

    def hgetall(self, name: str) -> Self:
        self._ops.append(("hgetall", name))
        return self

    def get(self, name: str) -> Self:
        self._ops.append(("get", name))
        return self

    async def execute(self) -> list[Any]:
        self._client.round_trips += 1
        return [self._client.reply(op, name) for op, name in self._ops]

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FakeRedis:
    """Serves a prebuilt keyspace as `bytes`, like a real client, and counts round trips."""

    def __init__(self, keyspace: dict[str, Any]) -> None:
        self.keyspace = keyspace
        self.round_trips = 0

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    def reply(self, op: str, name: str) -> Any:
        value = self.keyspace.get(name)
        if op == "get":
            return None if value is None else str(value).encode()
        if op == "hgetall":
            return {k.encode(): str(v).encode() for k, v in (value or {}).items()}
        return {str(v).encode() for v in (value or set())}


def _param(name: str, value: str, unit: str) -> dict[str, str]:
    return {"name": name, "value": value, "unit": unit}


def _keyspace(n_symbols: int = 1) -> dict[str, Any]:
    """A valid keyspace: `n_symbols` copies of a EUR/USD-like instrument."""
    keyspace: dict[str, Any] = {
        "calib:symbols": set(),
        "calib:quantities": {"return", "spread", "interval"},
        "symbology": {},
    }
    for i in range(n_symbols):
        venue, file_symbol = ("EUR/USD", "EURUSD") if i == 0 else (f"X{i}/USD", f"X{i}USD")
        keyspace["calib:symbols"].add(venue)
        keyspace["symbology"][venue] = file_symbol
        keyspace[f"instrument:{venue}"] = {
            "pip_size": "0.00001",
            "quote_currency": "USD",
            "initial_price": "1.15922",
        }
        fits = {
            "return": (
                "laplace",
                [_param("loc", "0", "quote"), _param("scale", "0.0000106012", "quote")],
            ),
            "spread": (
                "gamma",
                [
                    _param("a", "0.422882", "dimensionless"),
                    _param("loc", "0.999855", "pip"),
                    _param("scale", "4.92226", "pip"),
                ],
            ),
            "interval": (
                "lognormal",
                [
                    _param("s", "1.50353", "dimensionless"),
                    _param("loc", "-0.242662", "ms"),
                    _param("scale", "466.108", "ms"),
                ],
            ),
        }
        for quantity, (family, params) in fits.items():
            prefix = f"calib:{venue}:{quantity}"
            keyspace[f"{prefix}:family"] = family
            keyspace[f"{prefix}:param_count"] = str(len(params))
            for n, param in enumerate(params):
                keyspace[f"{prefix}:param:{n}"] = param
    return keyspace


def _load(keyspace: dict[str, Any]) -> dict:
    return asyncio.run(CalibrationStore(_FakeRedis(keyspace)).load())


# --- 2.1: the load ----------------------------------------------------------------------------


def test_load_converts_stored_units_to_code_units() -> None:
    calibration = _load(_keyspace())["EURUSD"]

    assert calibration.initial_price == Decimal("1.15922")
    assert calibration.price_increment == Decimal("0.00001")
    assert calibration.return_distribution == Distribution("laplace", (0.0, 1.06012e-05))
    assert calibration.spread_distribution == Distribution(
        "gamma", (0.422882, 9.99855e-06, 4.92226e-05)
    )
    assert calibration.interval_distribution == Distribution(
        "lognormal", (1.50353, -0.000242662, 0.466108)
    )


def test_price_increment_keeps_the_stored_exponent() -> None:
    # `_RandomWalk` quantizes to the increment's exponent, so `1E-5` must stay at exponent -5.
    calibration = _load(_keyspace())["EURUSD"]
    assert calibration.price_increment.as_tuple().exponent == -5


def test_load_is_keyed_by_file_symbol_in_sorted_order() -> None:
    assert list(_load(_keyspace(3))) == ["EURUSD", "X1USD", "X2USD"]


@pytest.mark.parametrize("n_symbols", [1, 5, 40])
def test_load_takes_three_round_trips_for_any_symbol_count(n_symbols: int) -> None:
    client = _FakeRedis(_keyspace(n_symbols))
    calibrations = asyncio.run(CalibrationStore(client).load())
    assert len(calibrations) == n_symbols
    assert client.round_trips == 3


# --- 2.2: fail fast, one test per condition -----------------------------------------------------


def _assert_rejected(keyspace: dict[str, Any], *fragments: str) -> None:
    with pytest.raises(CalibrationError) as excinfo:
        _load(keyspace)
    for fragment in fragments:
        assert fragment in str(excinfo.value)


def test_absent_symbol_set_means_not_seeded() -> None:
    keyspace = _keyspace()
    del keyspace["calib:symbols"]
    _assert_rejected(keyspace, "not seeded", "calib:symbols", "python -m tools.calibration_seed")


def test_empty_symbol_set_means_not_seeded() -> None:
    keyspace = _keyspace()
    keyspace["calib:symbols"] = set()
    _assert_rejected(keyspace, "not seeded", "calib:symbols")


def test_symbol_missing_from_symbology() -> None:
    keyspace = _keyspace()
    del keyspace["symbology"]["EUR/USD"]
    _assert_rejected(keyspace, "'EUR/USD'", "symbology")


def test_two_symbols_mapping_to_one_file_symbol() -> None:
    keyspace = _keyspace(2)
    keyspace["symbology"]["X1/USD"] = "EURUSD"
    _assert_rejected(keyspace, "'EURUSD'", "symbology")


@pytest.mark.parametrize("field", ["pip_size", "quote_currency", "initial_price"])
def test_missing_instrument_field(field: str) -> None:
    keyspace = _keyspace()
    del keyspace["instrument:EUR/USD"][field]
    _assert_rejected(keyspace, "'EUR/USD'", "instrument:EUR/USD", repr(field))


def test_missing_instrument_hash() -> None:
    keyspace = _keyspace()
    del keyspace["instrument:EUR/USD"]
    _assert_rejected(keyspace, "'EUR/USD'", "instrument:EUR/USD")


def test_non_numeric_instrument_field() -> None:
    keyspace = _keyspace()
    keyspace["instrument:EUR/USD"]["initial_price"] = "one"
    _assert_rejected(keyspace, "instrument:EUR/USD", "'initial_price'", "not a number")


def test_non_positive_pip_size() -> None:
    keyspace = _keyspace()
    keyspace["instrument:EUR/USD"]["pip_size"] = "0"
    _assert_rejected(keyspace, "'EUR/USD'", "'pip_size' must be positive")


def test_missing_quantity() -> None:
    keyspace = _keyspace()
    keyspace["calib:quantities"] = {"return", "interval"}
    _assert_rejected(keyspace, "calib:quantities", "spread")


def test_missing_family() -> None:
    keyspace = _keyspace()
    del keyspace["calib:EUR/USD:spread:family"]
    _assert_rejected(keyspace, "'EUR/USD'", "calib:EUR/USD:spread:family")


def test_family_the_adapter_cannot_sample() -> None:
    keyspace = _keyspace()
    keyspace["calib:EUR/USD:spread:family"] = "cauchy"
    _assert_rejected(keyspace, "'EUR/USD'", "calib:EUR/USD:spread:family", "'cauchy'")


def test_missing_param_count() -> None:
    keyspace = _keyspace()
    del keyspace["calib:EUR/USD:return:param_count"]
    _assert_rejected(keyspace, "'EUR/USD'", "calib:EUR/USD:return:param_count")


def test_param_count_higher_than_hashes_present() -> None:
    keyspace = _keyspace()
    keyspace["calib:EUR/USD:return:param_count"] = "3"
    _assert_rejected(keyspace, "'EUR/USD'", "param_count` is 3", "calib:EUR/USD:return:param:2")


def test_param_count_lower_than_hashes_present() -> None:
    keyspace = _keyspace()
    keyspace["calib:EUR/USD:spread:param_count"] = "2"
    _assert_rejected(keyspace, "'EUR/USD'", "param_count` is 2", "calib:EUR/USD:spread:param:2")


@pytest.mark.parametrize("field", ["name", "value", "unit"])
def test_parameter_missing_a_field(field: str) -> None:
    keyspace = _keyspace()
    del keyspace["calib:EUR/USD:interval:param:1"][field]
    _assert_rejected(keyspace, "'EUR/USD'", "calib:EUR/USD:interval:param:1", repr(field))


def test_unknown_unit() -> None:
    keyspace = _keyspace()
    keyspace["calib:EUR/USD:interval:param:2"]["unit"] = "s"
    _assert_rejected(keyspace, "'EUR/USD'", "calib:EUR/USD:interval:param:2", "unknown unit 's'")


def test_one_bad_symbol_rejects_the_whole_load() -> None:
    # No partial result: the healthy symbols are not returned either.
    keyspace = _keyspace(3)
    del keyspace["calib:X2/USD:spread:family"]
    _assert_rejected(keyspace, "'X2/USD'")
