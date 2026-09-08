"""Verification for task 2.4: `seq` is gapless within a session and resets across sessions.

The two guarantees are what make consumer-side gap detection meaningful, so both are exercised
directly, and the values a session produces are checked against the wire contract itself rather
than against an assumption about their shape.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tqtk_common import Tick
from tqtk_common.session import FeedSession

WIRE_DIR = Path(__file__).resolve().parents[3] / "contracts" / "wire"
SESSION_ID_SCHEMA = json.loads((WIRE_DIR / "tick.schema.json").read_text())["properties"][
    "session_id"
]


@pytest.fixture
def session() -> FeedSession:
    return FeedSession()


# --- gapless and monotonic within one session -----------------------------------------------


def test_the_first_seq_of_a_session_is_zero(session: FeedSession):
    assert session.next_seq("synthetic", "EURUSD") == 0


def test_seq_is_gapless_and_monotonic(session: FeedSession):
    issued = [session.next_seq("synthetic", "EURUSD") for _ in range(1000)]
    assert issued == list(range(1000))


def test_each_provider_symbol_pair_counts_independently(session: FeedSession):
    """Interleaving must not let one stream's traffic push another's counter along."""
    for _ in range(5):
        session.next_seq("synthetic", "EURUSD")
    assert session.next_seq("synthetic", "GBPJPY") == 0
    assert session.next_seq("synthetic", "EURUSD") == 5


def test_the_counter_is_keyed_on_provider_as_well_as_symbol(session: FeedSession):
    """Two providers quoting one symbol are two streams, never one interleaved counter."""
    for _ in range(3):
        session.next_seq("synthetic", "EURUSD")
    assert session.next_seq("dukascopy", "EURUSD") == 0
    assert session.next_seq("synthetic", "EURUSD") == 3


def test_interleaving_many_streams_keeps_every_counter_gapless(session: FeedSession):
    streams = [("synthetic", "EURUSD"), ("synthetic", "USDCHF"), ("dukascopy", "EURUSD")]
    issued: dict[tuple[str, str], list[int]] = {stream: [] for stream in streams}
    for _ in range(100):
        for stream in streams:
            issued[stream].append(session.next_seq(*stream))
    assert all(seqs == list(range(100)) for seqs in issued.values())


# --- reset on a new session ------------------------------------------------------------------


def test_a_new_session_resets_every_counter_to_zero():
    """A restart is a new session; the counter starts over rather than resuming."""
    first = FeedSession()
    for _ in range(42):
        first.next_seq("synthetic", "EURUSD")
    assert first.next_seq("synthetic", "EURUSD") == 42

    restarted = FeedSession()
    assert restarted.next_seq("synthetic", "EURUSD") == 0


def test_a_new_session_takes_a_new_identity():
    """Without this, a consumer would read the post-restart reset as catastrophic tick loss."""
    assert FeedSession().session_id != FeedSession().session_id


def test_session_ids_do_not_collide():
    assert len({FeedSession().session_id for _ in range(1000)}) == 1000


def test_the_identity_is_stable_for_the_life_of_the_session(session: FeedSession):
    before = session.session_id
    for _ in range(10):
        session.next_seq("synthetic", "EURUSD")
    assert session.session_id == before


# --- what it produces is contract-valid ------------------------------------------------------


def test_the_session_id_satisfies_the_wire_contract(session: FeedSession):
    assert re.match(SESSION_ID_SCHEMA["pattern"], session.session_id)
    assert len(session.session_id) <= SESSION_ID_SCHEMA["maxLength"]
    assert len(session.session_id) >= SESSION_ID_SCHEMA["minLength"]


def test_a_tick_stamped_from_a_session_validates_against_the_schema(session: FeedSession):
    tick = Tick(
        provider="synthetic",
        symbol="EURUSD",
        provider_ts=1788523200123000,
        recv_ts=1788523200123412,
        bid=1.17042,
        ask=1.17048,
        session_id=session.session_id,
        seq=session.next_seq("synthetic", "EURUSD"),
    )
    Draft202012Validator(json.loads((WIRE_DIR / "tick.schema.json").read_text())).validate(
        tick.to_dict()
    )


def test_the_dedup_key_is_unique_per_tick_within_a_session(session: FeedSession):
    """`(provider, symbol, session_id, seq)` is what consumers dedup on - it must not repeat."""
    keys = {
        ("synthetic", "EURUSD", session.session_id, session.next_seq("synthetic", "EURUSD"))
        for _ in range(500)
    }
    assert len(keys) == 500
