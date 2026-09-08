"""Redis stream and key names, built in one place instead of formatted at each call site.

`contracts/wire/*.schema.json` is the authority for the three layouts these functions produce:

    ticks.raw.{provider}.{symbol}
    bars.{timeframe}.{provider}.{symbol}
    bar_state:{provider}:{symbol}:{timeframe}

Every builder takes `(provider, symbol[, timeframe])` in that order regardless of the order the
name itself interpolates them, so a caller cannot silently swap two arguments by following the
string's shape rather than the signature's.

This is also where `provider` and `symbol` are checked against the contract's patterns. The record
models deliberately leave that to the schema, but here the fields are interpolated into names that
use `.` and `:` as separators, so a symbol like `EUR.USD` would not be rejected - it would quietly
produce a stream nobody discovers. The patterns are duplicated from the schemas rather than read
from them, because `contracts/` is not shipped inside a service image; a test pins the two copies
together.
"""

from __future__ import annotations

import re
from typing import Final

from tqtk_common.records import Timeframe

TICK_STREAM_PREFIX: Final = "ticks.raw"
BAR_STREAM_PREFIX: Final = "bars"
BAR_STATE_PREFIX: Final = "bar_state"

# Consumers discover streams rather than being configured with them, so a new provider's adapter
# is picked up without a consumer-side change. Both globs are deliberately unparameterised: no
# consumer in the pipeline reads one provider's or one timeframe's streams in isolation, and a
# provider-shaped argument that can be omitted misreads as a record that carries no provider.
TICK_STREAM_PATTERN: Final = f"{TICK_STREAM_PREFIX}.*"
BAR_STREAM_PATTERN: Final = f"{BAR_STREAM_PREFIX}.*.*"

# Mirrors `properties.provider.pattern` / `properties.symbol.pattern` in both wire schemas.
PROVIDER_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]*$"
SYMBOL_PATTERN: Final = r"^[A-Z0-9]{2,20}$"

_PROVIDER: Final = re.compile(PROVIDER_PATTERN)
_SYMBOL: Final = re.compile(SYMBOL_PATTERN)


def _checked_provider(provider: str) -> str:
    if not _PROVIDER.match(provider):
        raise ValueError(f"provider {provider!r} does not match {PROVIDER_PATTERN}")
    return provider


def _checked_symbol(symbol: str) -> str:
    if not _SYMBOL.match(symbol):
        raise ValueError(f"symbol {symbol!r} does not match {SYMBOL_PATTERN}")
    return symbol


def tick_stream(provider: str, symbol: str) -> str:
    """The raw-tick stream one feed adapter publishes for one symbol."""
    return f"{TICK_STREAM_PREFIX}.{_checked_provider(provider)}.{_checked_symbol(symbol)}"


def bar_stream(provider: str, symbol: str, timeframe: Timeframe | str) -> str:
    """The bar stream for one `(provider, symbol, timeframe)`, carrying both sides.

    `side` is a field of the record, never a segment of the name - see the `data-contract`
    capability - so one stream serves the bid and ask records alike.
    """
    return (
        f"{BAR_STREAM_PREFIX}.{Timeframe(timeframe)}"
        f".{_checked_provider(provider)}.{_checked_symbol(symbol)}"
    )


def bar_state_key(provider: str, symbol: str, timeframe: Timeframe | str) -> str:
    """The aggregation checkpoint for one actor, holding both of its side-bars."""
    return (
        f"{BAR_STATE_PREFIX}:{_checked_provider(provider)}"
        f":{_checked_symbol(symbol)}:{Timeframe(timeframe)}"
    )
