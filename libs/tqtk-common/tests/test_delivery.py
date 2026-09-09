"""Verification for task 2.5: gaps flagged, session changes not, duplicates deduped.

The three cases the task names are the contract's own scenarios, and each is exercised through a
real `Tick` rather than a stand-in, so the dedup key under test is the one records actually carry.
"""

from __future__ import annotations

import pytest

from tqtk_common import FeedSession, Tick
from tqtk_common.delivery import Delivery, DeliveryTracker, Observation

BASE_TS = 1788523200000000


def tick(session_id: str, seq: int, *, provider: str = "synthetic", symbol: str = "EURUSD") -> Tick:
    return Tick(
        provider=provider,
        symbol=symbol,
        provider_ts=BASE_TS + seq,
        recv_ts=BASE_TS + seq,
        bid=1.17042,
        ask=1.17048,
        session_id=session_id,
        seq=seq,
    )


@pytest.fixture
def tracker() -> DeliveryTracker:
    return DeliveryTracker()


@pytest.fixture
def session() -> str:
    return FeedSession().session_id


# --- an in-session gap is flagged -------------------------------------------------------------


def test_a_seq_jump_within_one_session_is_loss(tracker: DeliveryTracker, session: str):
    tracker.observe(tick(session, 0))
    observation = tracker.observe(tick(session, 5))
    assert observation.delivery is Delivery.GAP
    assert observation.is_loss
    assert observation.missing == 4


def test_the_gap_size_is_the_count_of_records_never_seen(tracker: DeliveryTracker, session: str):
    tracker.observe(tick(session, 10))
    assert tracker.observe(tick(session, 11)).missing == 0
    assert tracker.observe(tick(session, 13)).missing == 1


def test_a_gap_advances_the_mark_so_it_is_counted_once(tracker: DeliveryTracker, session: str):
    """The lost records must not be re-counted against every record that follows them."""
    tracker.observe(tick(session, 0))
    assert tracker.observe(tick(session, 100)).missing == 99
    assert tracker.observe(tick(session, 101)).delivery is Delivery.IN_ORDER


# --- a session change is not loss --------------------------------------------------------------


def test_a_new_session_id_is_a_restart_not_loss(tracker: DeliveryTracker):
    first, second = FeedSession(), FeedSession()
    for seq in range(50):
        tracker.observe(tick(first.session_id, seq))

    observation = tracker.observe(tick(second.session_id, 0))
    assert observation.delivery is Delivery.SESSION_CHANGE
    assert not observation.is_loss
    assert observation.missing == 0


def test_the_seq_reset_after_a_restart_is_not_read_as_a_duplicate(tracker: DeliveryTracker):
    """seq 0 is below the previous session's mark; keying on seq alone would drop the record."""
    first, second = FeedSession(), FeedSession()
    tracker.observe(tick(first.session_id, 41337))

    observation = tracker.observe(tick(second.session_id, 0))
    assert not observation.is_duplicate
    assert tracker.observe(tick(second.session_id, 1)).delivery is Delivery.IN_ORDER


def test_a_restart_resumes_gap_detection_in_the_new_session(tracker: DeliveryTracker):
    first, second = FeedSession(), FeedSession()
    tracker.observe(tick(first.session_id, 9))
    tracker.observe(tick(second.session_id, 0))
    assert tracker.observe(tick(second.session_id, 4)).missing == 3


def test_the_first_record_of_a_stream_is_not_loss(tracker: DeliveryTracker, session: str):
    """A consumer joining a stream mid-flight has lost nothing."""
    observation = tracker.observe(tick(session, 41337))
    assert observation.delivery is Delivery.FIRST
    assert not observation.is_loss
    assert observation.missing == 0


# --- duplicates are deduped ---------------------------------------------------------------------


def test_a_repeated_record_is_a_duplicate(tracker: DeliveryTracker, session: str):
    tracker.observe(tick(session, 0))
    tracker.observe(tick(session, 1))
    observation = tracker.observe(tick(session, 1))
    assert observation.delivery is Delivery.DUPLICATE
    assert observation.is_duplicate
    assert not observation.is_loss


def test_a_redelivered_batch_is_deduped_without_reopening_a_gap(
    tracker: DeliveryTracker, session: str
):
    """A crash-recovery replay re-sends records already handled; none may count as new or lost."""
    for seq in range(10):
        tracker.observe(tick(session, seq))
    replayed = [tracker.observe(tick(session, seq)) for seq in range(4, 10)]
    assert all(observation.is_duplicate for observation in replayed)
    assert tracker.observe(tick(session, 10)).delivery is Delivery.IN_ORDER


def test_an_older_seq_within_a_session_is_a_redelivery(tracker: DeliveryTracker, session: str):
    tracker.observe(tick(session, 100))
    assert tracker.observe(tick(session, 7)).is_duplicate


# --- the key is the whole tuple, per stream -----------------------------------------------------


def test_streams_are_tracked_independently(tracker: DeliveryTracker, session: str):
    tracker.observe(tick(session, 0, symbol="EURUSD"))
    assert tracker.observe(tick(session, 0, symbol="GBPJPY")).delivery is Delivery.FIRST
    assert tracker.observe(tick(session, 1, symbol="EURUSD")).delivery is Delivery.IN_ORDER


def test_one_symbol_from_two_providers_is_two_streams(tracker: DeliveryTracker, session: str):
    """Same symbol, same seq, different provider - never a duplicate of one another."""
    tracker.observe(tick(session, 0, provider="synthetic"))
    assert tracker.observe(tick(session, 0, provider="dukascopy")).delivery is Delivery.FIRST


def test_an_uninterrupted_stream_reports_no_loss(tracker: DeliveryTracker, session: str):
    observations = [tracker.observe(tick(session, seq)) for seq in range(1000)]
    assert observations[0].delivery is Delivery.FIRST
    assert all(o.delivery is Delivery.IN_ORDER for o in observations[1:])
    assert sum(o.missing for o in observations) == 0


def test_memory_is_bounded_by_stream_count_not_tick_volume(tracker: DeliveryTracker, session: str):
    """A high-water mark, not a set of every seq seen - a set would grow without bound."""
    for seq in range(10_000):
        tracker.observe(tick(session, seq))
    assert len(tracker._seen) == 1


def test_an_observation_is_immutable():
    observation = Observation(Delivery.GAP, 3)
    with pytest.raises(AttributeError):
        observation.missing = 0  # type: ignore[misc]
