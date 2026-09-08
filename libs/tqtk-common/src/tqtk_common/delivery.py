# TODO: Understand this module.

"""Consumer-side gap detection and deduplication, keyed on the contract's dedup tuple.

The producer side of this is `FeedSession`: `seq` is gapless per `(provider, symbol)` within a
session and resets to 0 on every adapter restart, paired with a `session_id`. A consumer therefore
cannot key on `seq` alone - it keys on `(provider, symbol, session_id, seq)` - and it reads the two
kinds of discontinuity differently: a `seq` jump inside one `session_id` is real tick loss, while a
`session_id` change is an expected restart and must not raise a loss alarm.

`DeliveryTracker` keeps a high-water mark per stream, not a set of every `seq` seen, so its memory
is bounded by the number of streams rather than the tick volume through them. That is sound
because Redis streams deliver in order and `seq` is monotonic within a session: a `seq` at or below
the mark can only be a redelivery.

The types here are plain frozen dataclasses rather than pydantic models. They are internal value
objects that never cross the wire, so they need no validation at a boundary - unlike the contract
records in `records.py`, which do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from tqtk_common.records import Tick

__all__ = ["Delivery", "DeliveryTracker", "Observation"]


class Delivery(StrEnum):
    """How one record relates to what the tracker has already seen on its stream."""

    FIRST = auto()
    """The first record for this `(provider, symbol)`. Not loss: the consumer joined a stream."""

    IN_ORDER = auto()
    """The expected next `seq` within the current session."""

    GAP = auto()
    """A `seq` jump inside one session: records were published that this consumer never saw."""

    DUPLICATE = auto()
    """A `seq` at or below the high-water mark - a redelivery, already accounted for."""

    SESSION_CHANGE = auto()
    """A new `session_id`: the adapter restarted, and the `seq` reset that follows is expected."""


@dataclass(frozen=True, slots=True)
class Observation:
    """What the tracker made of one record."""

    delivery: Delivery
    missing: int = 0

    @property
    def is_duplicate(self) -> bool:
        """Whether the consumer should skip this record as already handled."""
        return self.delivery is Delivery.DUPLICATE

    @property
    def is_loss(self) -> bool:
        """Whether this discontinuity should be counted and alerted as tick loss."""
        return self.delivery is Delivery.GAP


class DeliveryTracker:
    """Per-`(provider, symbol)` delivery state for one consumer.

    Not synchronised: one tracker belongs to one consumer task, mirroring the producer side.
    """

    __slots__ = ("_seen",)

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str], tuple[str, int]] = {}

    def observe(self, tick: Tick) -> Observation:
        """Classify `tick` against this stream's history, and advance the high-water mark."""
        key = (tick.provider, tick.symbol)
        previous = self._seen.get(key)

        if previous is None:
            self._seen[key] = (tick.session_id, tick.seq)
            return Observation(Delivery.FIRST)

        session_id, high_water = previous

        if session_id != tick.session_id:
            # An expected restart. A first observed `seq` above 0 here means the consumer joined
            # mid-session, or that entries were trimmed before it read them; neither is counted as
            # loss, because the contract makes a session change an expected discontinuity. Trimmed
            # entries surface through the bus-retention janitor's hard-cap alert instead.
            self._seen[key] = (tick.session_id, tick.seq)
            return Observation(Delivery.SESSION_CHANGE)

        if tick.seq <= high_water:
            return Observation(Delivery.DUPLICATE)

        self._seen[key] = (session_id, tick.seq)
        # Consecutive is `high_water + 1`, so `missing` is 0 and this is IN_ORDER; anything
        # further ahead leaves that many records the consumer never saw. It cannot go negative:
        # `seq <= high_water` returned DUPLICATE above.
        missing = tick.seq - high_water - 1
        if missing > 0:
            return Observation(Delivery.GAP, missing)
        return Observation(Delivery.IN_ORDER)
