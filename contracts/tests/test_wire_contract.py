"""The wire schemas are the contract; these tests are what make that claim checkable.

Every sample record under `contracts/wire/examples/` must validate against the schema its
filename names (`<schema>.<variant>.sample.json` -> `<schema>.schema.json`), and the schemas
themselves must be valid draft 2020-12. The samples double as the fixture set a non-Python
implementation validates against, so a Java adapter proves conformance on the same records
rather than on a re-typed copy of them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

WIRE_DIR = Path(__file__).resolve().parents[1] / "wire"
EXAMPLES_DIR = WIRE_DIR / "examples"

SCHEMA_PATHS = sorted(WIRE_DIR.glob("*.schema.json"))
EXAMPLE_PATHS = sorted(EXAMPLES_DIR.glob("*.sample.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def schema_for(example: Path) -> Path:
    """`bar.forming.sample.json` is a sample of `bar.schema.json`."""
    return WIRE_DIR / f"{example.name.split('.')[0]}.schema.json"


def validator_for(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(WIRE_DIR / f"{name}.schema.json"))


@pytest.fixture(scope="module")
def tick_sample() -> dict:
    return load(EXAMPLES_DIR / "tick.full.sample.json")


@pytest.fixture(scope="module")
def bar_sample() -> dict:
    return load(EXAMPLES_DIR / "bar.closed.sample.json")


@pytest.fixture(scope="module")
def bar_sample_ask() -> dict:
    return load(EXAMPLES_DIR / "bar.closed-ask.sample.json")


def test_wire_schemas_are_present():
    """A renamed or deleted schema must fail here, not silently skip every test below."""
    assert {path.name for path in SCHEMA_PATHS} == {"tick.schema.json", "bar.schema.json"}


@pytest.mark.parametrize("path", SCHEMA_PATHS, ids=lambda path: path.name)
def test_schema_is_valid_draft_2020_12(path: Path):
    Draft202012Validator.check_schema(load(path))


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda path: path.name)
def test_sample_record_validates(path: Path):
    schema_path = schema_for(path)
    assert schema_path.exists(), f"{path.name} names no known schema"
    Draft202012Validator(load(schema_path)).validate(load(path))


def test_every_schema_has_a_sample():
    """A schema no sample exercises is a schema nothing checks."""
    exercised = {path.name.split(".")[0] for path in EXAMPLE_PATHS}
    assert exercised == {path.name.split(".")[0] for path in SCHEMA_PATHS}


@pytest.mark.parametrize(
    "field",
    ["provider", "symbol", "provider_ts", "recv_ts", "bid", "ask", "session_id", "seq"],
)
def test_tick_rejects_a_missing_required_field(tick_sample: dict, field: str):
    record = {key: value for key, value in tick_sample.items() if key != field}
    assert not validator_for("tick").is_valid(record)


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "symbol",
        "side",
        "timeframe",
        "bar_start_ts",
        "open",
        "high",
        "low",
        "close",
        "tick_count",
        "is_closed",
        "last_update_ts",
    ],
)
def test_bar_rejects_a_missing_required_field(bar_sample: dict, field: str):
    record = {key: value for key, value in bar_sample.items() if key != field}
    assert not validator_for("bar").is_valid(record)


def test_tick_optional_fields_really_are_optional(tick_sample: dict):
    record = {
        key: value
        for key, value in tick_sample.items()
        if key not in {"provider_symbol", "bid_size", "ask_size"}
    }
    validator_for("tick").validate(record)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recv_ts", "1788523200123412"),  # a timestamp as a string, not epoch microseconds
        ("recv_ts", 1788523200.123412),  # seconds as a float
        ("seq", -1),  # uint64, never negative
        ("seq", 1.5),
        ("bid", 0),  # a price is strictly positive
        ("bid", "1.17042"),
        ("provider", "syn.thetic"),  # '.' separates the stream name's segments
        ("provider", "Synthetic"),  # stream names are lowercase
        ("provider", ""),
        ("symbol", "eurusd"),
        ("symbol", "EUR/USD"),  # that is provider_symbol's job
        ("session_id", ""),
    ],
)
def test_tick_rejects_a_malformed_field(tick_sample: dict, field: str, value: object):
    assert not validator_for("tick").is_valid({**tick_sample, field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("timeframe", "2m"),  # not one of the eight configured timeframes
        ("timeframe", "1d"),  # the enum is case-sensitive: 1D
        ("side", "mid"),  # the enum is closed at two: mid is derived, never published
        ("side", "BID"),  # case-sensitive, like every other enum here
        ("side", ""),
        ("side", None),
        ("tick_count", -1),
        ("is_closed", "true"),  # a JSON boolean, not a string
        ("bar_start_ts", -1),
        ("high", None),
    ],
)
def test_bar_rejects_a_malformed_field(bar_sample: dict, field: str, value: object):
    assert not validator_for("bar").is_valid({**bar_sample, field: value})


def test_all_eight_timeframes_are_accepted(bar_sample: dict):
    validator = validator_for("bar")
    for timeframe in ("1s", "1m", "5m", "15m", "30m", "1h", "4h", "1D"):
        validator.validate({**bar_sample, "timeframe": timeframe})


def test_both_sides_are_accepted(bar_sample: dict):
    validator = validator_for("bar")
    for side in ("bid", "ask"):
        validator.validate({**bar_sample, "side": side})


def test_the_two_sides_of_one_window_share_an_identity(bar_sample: dict, bar_sample_ask: dict):
    """A window's two records differ in `side` and its prices, in nothing else.

    `tick_count` is equal because one tick supplies both sides, and the rest of the identity is
    what a consumer pairs on after a batched read splits them.
    """
    assert bar_sample["side"] == "bid"
    assert bar_sample_ask["side"] == "ask"
    identity = ("provider", "symbol", "timeframe", "bar_start_ts")
    assert all(bar_sample[key] == bar_sample_ask[key] for key in identity)
    assert bar_sample["tick_count"] == bar_sample_ask["tick_count"]
    assert bar_sample["is_closed"] == bar_sample_ask["is_closed"]


@pytest.mark.parametrize("path", EXAMPLE_PATHS, ids=lambda path: path.name)
def test_unknown_fields_are_tolerated(path: Path):
    """Forward compatibility: a producer may add a field before consumers adopt it."""
    record = {**load(path), "field_added_by_a_newer_producer": 1}
    Draft202012Validator(load(schema_for(path))).validate(record)
