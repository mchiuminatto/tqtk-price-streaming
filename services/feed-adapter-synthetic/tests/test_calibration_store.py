"""Verification for tasks 2.1 and 2.2: `CalibrationStore` loads in three round trips, converts
stored units by `unit` alone, and refuses an incomplete store naming the symbol and key - and for
the committed seed script (`deploy/calibration/calibration.redis`): it is one transaction of plain
writes, it loads cleanly, and every parameter carries the right unit.

Most keyspaces here are built inline, so those stay tests of the read side; the seed-script
section parses the real file the `calibration-seeder` container applies.
"""

from __future__ import annotations

import asyncio
import shlex
from decimal import Decimal
from pathlib import Path
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
    _assert_rejected(keyspace, "not seeded", "calib:symbols", "calibration-seeder")


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


# --- the committed seed script ----------------------------------------------------------------

_SEED_SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "calibration" / "calibration.redis"
_DELETE_PREVIOUS_SEEDING = ["calib:*", "instrument:*", "symbology"]
_LOCATION_SCALE_UNITS = {"return": "quote", "spread": "pip", "interval": "ms"}


def _seed_commands() -> list[list[str]]:
    """The script's commands as `redis-cli` receives them: the seeder strips comment and blank
    lines (`grep -Ev '^[[:space:]]*(#|$)'`) and redis-cli splits each line like a shell would."""
    lines = (line.strip() for line in _SEED_SCRIPT.read_text().splitlines())
    return [shlex.split(line) for line in lines if line and not line.startswith("#")]


def _keyspace_from_seed_script() -> dict[str, Any]:
    keyspace: dict[str, Any] = {}
    for command, *args in _seed_commands()[2:-1]:
        if command == "SET":
            name, value = args
            keyspace[name] = value
        elif command == "HSET":
            name, *pairs = args
            keyspace.setdefault(name, {}).update(zip(pairs[0::2], pairs[1::2], strict=True))
        elif command == "SADD":
            name, *members = args
            keyspace.setdefault(name, set()).update(members)
        else:
            raise AssertionError(f"unexpected command in the seed script body: {command}")
    return keyspace


def test_seed_script_is_one_transaction_that_first_deletes_the_previous_seeding() -> None:
    commands = _seed_commands()

    assert commands[0] == ["MULTI"]
    assert commands[1][0] == "EVAL"
    assert commands[1][2:] == ["0", *_DELETE_PREVIOUS_SEEDING]
    assert commands[-1] == ["EXEC"]
    assert {command for command, *_ in commands[2:-1]} <= {"SET", "HSET", "SADD"}


def test_seed_script_loads_every_symbol() -> None:
    # `load` validates the whole keyspace - families, parameter counts, units, instrument fields -
    # so loading cleanly is the completeness check.
    calibrations = _load(_keyspace_from_seed_script())

    assert len(calibrations) == 17
    assert "EURUSD" in calibrations


def test_seed_script_tags_every_parameter_with_its_unit() -> None:
    keyspace = _keyspace_from_seed_script()
    params = [
        (key.split(":")[2], fields)
        for key, fields in keyspace.items()
        if key.startswith("calib:") and ":param:" in key
    ]

    assert params
    for quantity, fields in params:
        expected = (
            _LOCATION_SCALE_UNITS[quantity]
            if fields["name"] in ("loc", "scale")
            else "dimensionless"
        )
        assert fields["unit"] == expected, (quantity, fields)


def test_seed_script_numbers_are_plain_decimals() -> None:
    for key, fields in _keyspace_from_seed_script().items():
        if key.startswith("instrument:") or ":param:" in key:
            for field in ("pip_size", "initial_price", "value"):
                if field in fields:
                    assert "e" not in fields[field].lower(), (key, field, fields[field])
                    Decimal(fields[field])
