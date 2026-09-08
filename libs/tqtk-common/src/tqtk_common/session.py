"""Feed-adapter session identity and the per-`(provider, symbol)` tick counters.

The `data-contract` capability makes `seq` a `uint64` that is monotonic and gapless per
`(provider, symbol)` *within one session*, resetting to 0 whenever an adapter (re)starts, paired
with a `session_id` naming that session. That pairing is what lets a consumer tell real tick loss
apart from an expected restart: a `seq` jump inside one `session_id` is loss, a `session_id` change
is not.

The counter is deliberately never persisted. A durable write on the tick hot path would cost
latency and need reconciliation after a crash, and buys nothing a session-scoped reset does not
already give - see `design.md`, "`seq`/`session_id`: session-scoped reset, not a persisted global
counter".

One `FeedSession` belongs to one adapter process. It is not synchronised: `seq` assignment for a
given `(provider, symbol)` must stay on a single thread or event loop, which is how the adapters
are built. Sharing one across threads would break the gapless guarantee it exists to provide.
"""

from __future__ import annotations

from uuid import uuid4

__all__ = ["FeedSession"]


class FeedSession:
    """One adapter session: a fresh identity, and a counter per `(provider, symbol)` from 0.

    Construct exactly one per process start. Constructing a second one *is* a new session, which
    is the same thing a restart means to a consumer.
    """

    __slots__ = ("_next", "_session_id")

    def __init__(self) -> None:
        # Consumers only ever compare `session_id` for equality, so a random identifier is
        # enough; nothing reads an ordering or a timestamp out of it. 32 hex characters satisfy
        # the contract's `session_id` pattern and length bound.
        self._session_id = uuid4().hex
        self._next: dict[tuple[str, str], int] = {}

    @property
    def session_id(self) -> str:
        return self._session_id

    def next_seq(self, provider: str, symbol: str) -> int:
        """The next `seq` for this `(provider, symbol)`, starting at 0 and never skipping.

        Not clamped to `uint64`: at any plausible tick rate the counter would take longer than
        the age of the universe to reach the ceiling, and `Tick` rejects an out-of-range value
        rather than letting a wrapped one onto the bus.
        """
        key = (provider, symbol)
        seq = self._next.get(key, 0)
        self._next[key] = seq + 1
        return seq
