"""Verification for task 2.3: every naming pattern in the data contract, built and checked.

The layouts asserted here are the ones the `data-contract` capability fixes. The provider and
symbol patterns are pinned to the wire schemas rather than restated, so the copy in `names.py`
cannot drift from the contract it enforces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tqtk_common import Timeframe
from tqtk_common.names import (
    BAR_STREAM_PATTERN,
    PROVIDER_PATTERN,
    SYMBOL_PATTERN,
    TICK_STREAM_PATTERN,
    bar_state_key,
    bar_stream,
    tick_stream,
)

WIRE_DIR = Path(__file__).resolve().parents[3] / "contracts" / "wire"


def schema_pattern(schema: str, field: str) -> str:
    properties = json.loads((WIRE_DIR / f"{schema}.schema.json").read_text())["properties"]
    return properties[field]["pattern"]


# --- the three layouts ----------------------------------------------------------------------


def test_tick_stream_name():
    assert tick_stream("synthetic", "EURUSD") == "ticks.raw.synthetic.EURUSD"


def test_bar_stream_name():
    assert bar_stream("synthetic", "EURUSD", Timeframe.M1) == "bars.1m.synthetic.EURUSD"


def test_bar_state_key_name():
    assert bar_state_key("synthetic", "EURUSD", Timeframe.M1) == "bar_state:synthetic:EURUSD:1m"


def test_the_builders_share_one_argument_order():
    """Interpolation order differs between the three; the signatures deliberately do not."""
    args = ("dukascopy", "GBPJPY", Timeframe.H4)
    assert bar_stream(*args) == "bars.4h.dukascopy.GBPJPY"
    assert bar_state_key(*args) == "bar_state:dukascopy:GBPJPY:4h"


@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_every_timeframe_appears_verbatim(timeframe: Timeframe):
    assert bar_stream("synthetic", "EURUSD", timeframe).split(".")[1] == timeframe.value
    assert bar_state_key("synthetic", "EURUSD", timeframe).split(":")[-1] == timeframe.value


def test_a_timeframe_may_be_given_as_its_contract_string():
    assert bar_stream("synthetic", "EURUSD", "15m") == bar_stream(
        "synthetic", "EURUSD", Timeframe.M15
    )


def test_an_unknown_timeframe_is_refused():
    with pytest.raises(ValueError):
        bar_stream("synthetic", "EURUSD", "2m")


def test_one_bar_stream_carries_both_sides():
    """`side` is a record field, never a name segment - the stream is the same for either."""
    assert "bid" not in bar_stream("synthetic", "EURUSD", Timeframe.S1)
    assert bar_stream("synthetic", "EURUSD", Timeframe.S1).count(".") == 3


# --- discovery globs ------------------------------------------------------------------------


def test_the_discovery_globs():
    assert TICK_STREAM_PATTERN == "ticks.raw.*"
    assert BAR_STREAM_PATTERN == "bars.*.*"


@pytest.mark.parametrize("provider", ["synthetic", "dukascopy"])
@pytest.mark.parametrize("timeframe", list(Timeframe))
def test_a_built_name_is_matched_by_its_own_discovery_glob(provider: str, timeframe: Timeframe):
    """Redis globs, checked with fnmatch: the same semantics for `*` across a flat key space.

    Discovery spans every provider by design - the pipeline is provider-count-agnostic, and a
    consumer must pick up a new adapter's streams without a configuration change.
    """
    from fnmatch import fnmatch

    assert fnmatch(tick_stream(provider, "EURUSD"), TICK_STREAM_PATTERN)
    assert fnmatch(bar_stream(provider, "EURUSD", timeframe), BAR_STREAM_PATTERN)


def test_the_tick_glob_does_not_match_a_bar_stream():
    """`ticks.raw.*` and `bars.*.*` share a key space; neither may sweep up the other's."""
    from fnmatch import fnmatch

    assert not fnmatch(bar_stream("synthetic", "EURUSD", Timeframe.M1), TICK_STREAM_PATTERN)
    assert not fnmatch(tick_stream("synthetic", "EURUSD"), BAR_STREAM_PATTERN)
    assert not fnmatch(bar_state_key("synthetic", "EURUSD", Timeframe.M1), BAR_STREAM_PATTERN)


# --- the separators these names are made of -------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    [
        "EUR.USD",  # '.' separates the stream name's segments
        "EUR:USD",  # ':' separates the key's
        "EUR/USD",  # that is provider_symbol's job
        "eurusd",  # symbols are upper-case
        "E",  # below the 2-character floor
        "E" * 21,  # above the 20-character ceiling
        "",
        "EURUSD ",
    ],
)
def test_a_symbol_that_would_corrupt_a_name_is_refused(symbol: str):
    with pytest.raises(ValueError, match="symbol"):
        tick_stream("synthetic", symbol)


@pytest.mark.parametrize(
    "provider",
    ["syn.thetic", "syn:thetic", "Synthetic", "", "-synthetic", "synthetic feed"],
)
def test_a_provider_that_would_corrupt_a_name_is_refused(provider: str):
    with pytest.raises(ValueError, match="provider"):
        tick_stream(provider, "EURUSD")


@pytest.mark.parametrize(
    "builder",
    [
        tick_stream,
        lambda p, s: bar_stream(p, s, Timeframe.M1),
        lambda p, s: bar_state_key(p, s, Timeframe.M1),
    ],
    ids=["tick_stream", "bar_stream", "bar_state_key"],
)
def test_every_builder_validates_not_only_the_first(builder):
    with pytest.raises(ValueError, match="symbol"):
        builder("synthetic", "EUR.USD")
    with pytest.raises(ValueError, match="provider"):
        builder("syn.thetic", "EURUSD")


# --- the patterns are the contract's, not this module's --------------------------------------


@pytest.mark.parametrize("schema", ["tick", "bar"])
@pytest.mark.parametrize(
    ("field", "pattern"), [("provider", PROVIDER_PATTERN), ("symbol", SYMBOL_PATTERN)]
)
def test_the_patterns_match_the_wire_schemas(schema: str, field: str, pattern: str):
    """`names.py` hardcodes these because contracts/ is not shipped in a service image.

    That is a second copy, so it gets the same treatment as the field lists: pinned by a test
    rather than by anyone remembering to change both.
    """
    assert schema_pattern(schema, field) == pattern
