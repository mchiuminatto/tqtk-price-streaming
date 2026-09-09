"""The Python implementation of the `Tick`/`Bar` wire contract.

`contracts/wire/*.schema.json` is the authority, not this module: these models conform to those
files, and the tests hold the two together field by field rather than by anyone remembering to
update both. A future Java adapter implements the same files without importing this package.

Timestamps are integer microseconds since the Unix epoch, UTC. Two serializations exist because
two callers need different things: `to_dict`/`from_dict` is the typed logical record the schemas
describe, and `to_wire`/`from_wire` is the flat string map a Redis stream entry actually carries.
Parsing that string map back is `model_validate` in lax mode, so both directions are one call.

Validation runs at construction, which is what keeps a malformed record off the bus - so build a
changed record with `Model.model_validate({**old.to_dict(), ...})` rather than `model_copy`, which
by design does not re-validate.

Single-field shape the schema already pins down (the `provider`/`symbol` patterns) is deliberately
not repeated here: duplicating those regexes would only give them somewhere to drift apart.
Records are schema-checked at the boundary, and the field-parity test fails if the two sets of
field names ever diverge.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MICROS_PER_SECOND: Final = 1_000_000
UINT64_MAX: Final = 2**64 - 1

Price = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Size = Annotated[float, Field(ge=0, allow_inf_nan=False)]
EpochMicros = Annotated[int, Field(ge=0)]
Name = Annotated[str, Field(min_length=1)]


class Side(StrEnum):
    """Which price of a tick a bar's OHLC was built from.

    Closed at two: a mid series is derivable by consumers and is never published.
    """

    BID = "bid"
    ASK = "ask"


class Timeframe(StrEnum):
    """The eight bar widths every tick updates."""

    S1 = "1s"
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1D"

    @property
    def micros(self) -> int:
        return _TIMEFRAME_SECONDS[self] * MICROS_PER_SECOND


# Every timeframe divides a day exactly and the epoch falls on a midnight boundary, so a window
# start is aligned iff `bar_start_ts % timeframe.micros == 0` - true for 1D as much as for 1s.
_TIMEFRAME_SECONDS: Final[dict[Timeframe, int]] = {
    Timeframe.S1: 1,
    Timeframe.M1: 60,
    Timeframe.M5: 5 * 60,
    Timeframe.M15: 15 * 60,
    Timeframe.M30: 30 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
}


def _to_wire_value(value: Any) -> str:
    # `str(float)` round-trips exactly in Python 3; booleans take the JSON spelling so a human
    # reading `XRANGE` output sees what the schema describes.
    return "true" if value is True else "false" if value is False else str(value)


class Record(BaseModel):
    """Shared serialization for both contract records.

    `extra="ignore"` mirrors the schemas' `additionalProperties: true`: a producer may publish a
    field this build has never heard of, and dropping it beats refusing the record.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    def to_dict(self) -> dict[str, Any]:
        """The typed logical record, in the schema's own field order."""
        return self.model_dump(mode="json", exclude_none=True)

    @classmethod
    def from_dict(cls, record: dict[str, Any]) -> Self:
        return cls.model_validate(record)

    def to_wire(self) -> dict[str, str]:
        """A Redis field map. Absent optional fields are omitted, never sent as empty strings."""
        return {key: _to_wire_value(value) for key, value in self.to_dict().items()}

    @classmethod
    def from_wire(cls, raw: dict[str, str]) -> Self:
        """Lax-mode coercion turns the string map back into typed fields - no parse table."""
        return cls.model_validate(raw)


class Tick(Record):
    """One raw quote from one provider for one symbol."""

    provider: Name
    symbol: Name
    provider_symbol: Name | None = None
    provider_ts: EpochMicros
    recv_ts: EpochMicros
    bid: Price
    ask: Price
    bid_size: Size | None = None
    ask_size: Size | None = None
    session_id: Name
    seq: Annotated[int, Field(ge=0, le=UINT64_MAX)]

    @model_validator(mode="after")
    def _check_quote(self) -> Self:
        if self.ask < self.bid:
            raise ValueError(f"ask {self.ask} is below bid {self.bid}")
        return self

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    def price(self, side: Side) -> float:
        """The price a bar of `side` folds in - the one place tick-to-bar side mapping lives."""
        return self.bid if side is Side.BID else self.ask


class Bar(Record):
    """One side's OHLCV state for one `(provider, symbol, side, timeframe)` window.

    A window produces two of these - one per `Side` - carrying the same `tick_count`, since every
    tick supplies both a bid and an ask. They are published together and paired by consumers on
    `(provider, symbol, timeframe, bar_start_ts)`.
    """

    provider: Name
    symbol: Name
    side: Side
    timeframe: Timeframe
    bar_start_ts: EpochMicros
    open: Price
    high: Price
    low: Price
    close: Price
    tick_count: Annotated[int, Field(ge=0)]
    is_closed: bool
    last_update_ts: EpochMicros

    @model_validator(mode="after")
    def _check_window(self) -> Self:
        if self.high < self.low:
            raise ValueError(f"high {self.high} is below low {self.low}")
        if self.high < max(self.open, self.close):
            raise ValueError(f"high {self.high} is below open {self.open} / close {self.close}")
        if self.low > min(self.open, self.close):
            raise ValueError(f"low {self.low} is above open {self.open} / close {self.close}")
        if self.bar_start_ts % self.timeframe.micros:
            raise ValueError(
                f"bar_start_ts {self.bar_start_ts} is not aligned to a {self.timeframe} boundary"
            )
        if self.last_update_ts < self.bar_start_ts:
            raise ValueError(
                f"last_update_ts {self.last_update_ts} precedes bar_start_ts {self.bar_start_ts}"
            )
        return self

    @property
    def identity(self) -> tuple[str, str, Side, Timeframe, int]:
        """What persistence upserts on and the gateway reconciles on."""
        return (self.provider, self.symbol, self.side, self.timeframe, self.bar_start_ts)

    @property
    def window_key(self) -> tuple[str, str, Timeframe, int]:
        """Identity without the side: what a consumer pairs the two side-records on."""
        return (self.provider, self.symbol, self.timeframe, self.bar_start_ts)

    @property
    def is_idle(self) -> bool:
        return self.tick_count == 0
