"""Verification for task 2.2: the Python models conform to the wire contract and round-trip.

Conformance is checked against `contracts/wire/*.schema.json` themselves, and every fixture
record under `contracts/wire/examples/` is loaded through the models, so this suite fails if the
contract and its Python implementation ever drift - not only if this file's own expectations do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tqtk_common import UINT64_MAX, Bar, Record, Side, Tick, Timeframe

CONTRACTS_DIR = Path(__file__).resolve().parents[3] / "contracts" / "wire"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"


def load(path: Path) -> Any:
    return json.loads(path.read_text())


def mutate[T: Record](record: T, **changes: Any) -> T:
    """A changed copy, re-validated.

    Deliberately not `model_copy(update=...)`: that skips validation by design, so every
    invariant test below would pass against a model that no longer enforces anything.
    """
    return type(record).model_validate({**record.to_dict(), **changes})


def validator_for(name: str) -> Draft202012Validator:
    return Draft202012Validator(load(CONTRACTS_DIR / f"{name}.schema.json"))


def examples(prefix: str) -> list[Path]:
    found = sorted(EXAMPLES_DIR.glob(f"{prefix}.*.sample.json"))
    assert found, f"no {prefix} fixtures found under {EXAMPLES_DIR}"
    return found


TICK_EXAMPLES = examples("tick")
BAR_EXAMPLES = examples("bar")


@pytest.fixture
def tick() -> Tick:
    return Tick(
        provider="synthetic",
        symbol="EURUSD",
        provider_symbol="EUR/USD",
        provider_ts=1788523200123000,
        recv_ts=1788523200123412,
        bid=1.17042,
        ask=1.17048,
        bid_size=1.5,
        ask_size=2.25,
        session_id="01J9Z6XK7Q8V3N2M4P5R6S7T8U",
        seq=41337,
    )


@pytest.fixture
def bar() -> Bar:
    return Bar(
        provider="synthetic",
        symbol="EURUSD",
        side=Side.BID,
        timeframe=Timeframe.M1,
        bar_start_ts=1788523200000000,
        open=1.17042,
        high=1.17061,
        low=1.17033,
        close=1.17055,
        tick_count=318,
        is_closed=True,
        last_update_ts=1788523259871004,
    )


# --- conformance to the schema files -------------------------------------------------------


def schema_fields(name: str) -> tuple[set[str], set[str]]:
    schema = load(CONTRACTS_DIR / f"{name}.schema.json")
    return set(schema["properties"]), set(schema["required"])


def model_fields(cls: type[Record]) -> tuple[set[str], set[str]]:
    """Field names and the required subset, read off the class rather than restated here."""
    declared = cls.model_fields
    return (
        set(declared),
        {name for name, field in declared.items() if field.is_required()},
    )


@pytest.mark.parametrize(("name", "cls"), [("tick", Tick), ("bar", Bar)])
def test_the_model_declares_exactly_the_schema_s_fields(name: str, cls: type[Record]):
    """The contract lives in two places by design; this is what keeps them from drifting.

    Validating a serialized record catches a model that emits something wrong, but not a schema
    that grows an optional field no implementation has yet - and never a model field the schema
    does not define, since `additionalProperties: true` accepts it. Comparing the two field sets
    directly catches both directions.
    """
    schema_all, schema_required = schema_fields(name)
    model_all, model_required = model_fields(cls)
    assert model_all == schema_all
    assert model_required == schema_required


def test_tick_serializes_to_a_record_the_schema_accepts(tick: Tick):
    validator_for("tick").validate(tick.to_dict())


def test_bar_serializes_to_a_record_the_schema_accepts(bar: Bar):
    validator_for("bar").validate(bar.to_dict())


def test_a_tick_without_its_optional_fields_still_conforms():
    minimal = Tick(
        provider="synthetic",
        symbol="GBPJPY",
        provider_ts=1788523200456000,
        recv_ts=1788523200456877,
        bid=198.412,
        ask=198.427,
        session_id="01J9Z6XK7Q8V3N2M4P5R6S7T8U",
        seq=0,
    )
    record = minimal.to_dict()
    assert not {"provider_symbol", "bid_size", "ask_size"} & record.keys()
    validator_for("tick").validate(record)


@pytest.mark.parametrize("path", TICK_EXAMPLES, ids=lambda path: path.name)
def test_every_tick_fixture_loads_and_re_emits_unchanged(path: Path):
    record = load(path)
    assert Tick.from_dict(record).to_dict() == record


@pytest.mark.parametrize("path", BAR_EXAMPLES, ids=lambda path: path.name)
def test_every_bar_fixture_loads_and_re_emits_unchanged(path: Path):
    record = load(path)
    assert Bar.from_dict(record).to_dict() == record


def test_the_two_side_fixtures_of_one_window_pair_up():
    bid = Bar.from_dict(load(EXAMPLES_DIR / "bar.closed.sample.json"))
    ask = Bar.from_dict(load(EXAMPLES_DIR / "bar.closed-ask.sample.json"))
    assert bid.side is Side.BID and ask.side is Side.ASK
    assert bid.window_key == ask.window_key
    assert bid.identity != ask.identity
    assert bid.tick_count == ask.tick_count


# --- round-trips ---------------------------------------------------------------------------


def test_tick_round_trips_through_the_wire_form(tick: Tick):
    assert Tick.from_wire(tick.to_wire()) == tick


def test_bar_round_trips_through_the_wire_form(bar: Bar):
    assert Bar.from_wire(bar.to_wire()) == bar


def test_the_wire_form_is_a_flat_string_map(bar: Bar):
    """Redis stream entries carry strings; nothing may reach XADD as a Python object."""
    assert all(isinstance(value, str) for value in bar.to_wire().values())
    assert bar.to_wire()["is_closed"] == "true"
    assert bar.to_wire()["side"] == "bid"


def test_absent_optional_fields_are_omitted_from_the_wire_form(tick: Tick):
    wire = mutate(tick, provider_symbol=None, bid_size=None, ask_size=None).to_wire()
    assert not {"provider_symbol", "bid_size", "ask_size"} & wire.keys()


def test_a_zero_size_survives_the_round_trip(tick: Tick):
    """0.0 is a real size, not an absent one - the omission rule keys on None."""
    zeroed = mutate(tick, bid_size=0.0)
    assert zeroed.to_wire()["bid_size"] == "0.0"
    assert Tick.from_wire(zeroed.to_wire()).bid_size == 0.0


def test_an_unknown_field_from_a_newer_producer_is_dropped_not_refused(bar: Bar):
    wire = {**bar.to_wire(), "field_added_by_a_newer_producer": "1"}
    assert Bar.from_wire(wire) == bar


@pytest.mark.parametrize("side", list(Side))
@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_every_side_and_timeframe_round_trips(bar: Bar, side: Side, timeframe: Timeframe):
    variant = mutate(bar, side=side, timeframe=timeframe, bar_start_ts=0, last_update_ts=0)
    assert Bar.from_wire(variant.to_wire()) == variant
    validator_for("bar").validate(variant.to_dict())


# --- the invariants JSON Schema cannot express ---------------------------------------------


def test_tick_rejects_an_ask_below_its_bid(tick: Tick):
    with pytest.raises(ValueError, match="below bid"):
        mutate(tick, ask=tick.bid - 0.00001)


def test_tick_accepts_a_zero_spread(tick: Tick):
    assert mutate(tick, ask=tick.bid).spread == 0


def test_tick_rejects_a_seq_outside_uint64(tick: Tick):
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        mutate(tick, seq=-1)
    with pytest.raises(ValueError, match=f"less than or equal to {UINT64_MAX}"):
        mutate(tick, seq=UINT64_MAX + 1)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("high", 1.17050, "high .* is below open"),  # below the open it must cover
        ("low", 1.17045, "low .* is above open"),
        ("tick_count", -1, "tick_count"),
        ("last_update_ts", 1788523199000000, "precedes bar_start_ts"),
        ("open", 0.0, "greater than 0"),  # a price is strictly positive
    ],
)
def test_bar_rejects_a_broken_ohlc_invariant(bar: Bar, field: str, value: object, message: str):
    with pytest.raises(ValueError, match=message):
        mutate(bar, **{field: value})


def test_bar_rejects_a_misaligned_window_start(bar: Bar):
    with pytest.raises(ValueError, match="not aligned"):
        mutate(bar, bar_start_ts=bar.bar_start_ts + 1)


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_alignment_is_checked_against_the_bar_s_own_timeframe(timeframe: Timeframe):
    """A 1D-aligned start is legal on every timeframe; a 1s-aligned one is not, above 1s."""
    midnight = Timeframe.D1.micros
    aligned = Bar(
        provider="synthetic",
        symbol="EURUSD",
        side=Side.ASK,
        timeframe=timeframe,
        bar_start_ts=midnight,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        tick_count=0,
        is_closed=True,
        last_update_ts=midnight,
    )
    assert aligned.is_idle
    if timeframe is not Timeframe.S1:
        with pytest.raises(ValueError, match="not aligned"):
            mutate(aligned, bar_start_ts=midnight + Timeframe.S1.micros)


def test_an_idle_bar_carrying_a_flat_price_is_valid(bar: Bar):
    idle = mutate(bar, open=1.17055, high=1.17055, low=1.17055, close=1.17055, tick_count=0)
    assert idle.is_idle
    validator_for("bar").validate(idle.to_dict())


# --- side and timeframe are closed enums ----------------------------------------------------


def test_mid_is_not_a_side():
    with pytest.raises(ValueError):
        Side("mid")


def test_an_unknown_timeframe_is_refused():
    with pytest.raises(ValueError):
        Timeframe("2m")


def test_timeframe_micros_cover_the_eight_widths():
    assert [tf.micros for tf in Timeframe] == [
        1_000_000,
        60_000_000,
        300_000_000,
        900_000_000,
        1_800_000_000,
        3_600_000_000,
        14_400_000_000,
        86_400_000_000,
    ]


def test_tick_price_maps_each_side_to_its_own_quote(tick: Tick):
    assert tick.price(Side.BID) == tick.bid
    assert tick.price(Side.ASK) == tick.ask
