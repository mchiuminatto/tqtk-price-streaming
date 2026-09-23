"""Build the calibration keyspace from `tables.py` and write it to Redis.

Key layout (the `calibration-store` capability owns these names):

- `symbology`                             hash: venue symbol -> file symbol
- `instrument:<venue>`                    hash: `pip_size`, `quote_currency`, `initial_price`
- `calib:<venue>:<quantity>:family`       string: fitted family name
- `calib:<venue>:<quantity>:param_count`  string: number of parameters
- `calib:<venue>:<quantity>:param:<n>`    hash: `name`, `value`, `unit`, `n` from 0 in fit order
- `calib:symbols`                         set: every venue symbol
- `calib:quantities`                      set: `return`, `spread`, `interval`

Stored units differ from `tables.py`'s code units: spreads are stored in pips (multiples of the
instrument's `pip_size`), tick intervals in milliseconds, returns unchanged in quote units. Only
`loc` and `scale` carry a unit; shape parameters are `dimensionless` and never scaled. The
conversion is done in `Decimal` on each float's shortest round-tripping decimal string, so it is an
exact decimal shift (`pip_size` is a power of ten, `1000` exact) and the reader's inverse
reproduces every table value exactly - float arithmetic (`x / pip * pip`) would not.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

from .tables import (
    INSTRUMENTS,
    INTERVAL_FITS,
    PARAM_NAMES,
    RETURN_FITS,
    SPREAD_FITS,
    SYMBOLOGY,
    Fit,
    Instrument,
)

__all__ = [
    "KEY_PATTERNS",
    "QUANTITIES",
    "Keyspace",
    "SeedClient",
    "build_keyspace",
    "seed",
    "to_stored",
]

# Quantity name -> the unit its `loc`/`scale` parameters are stored in.
QUANTITIES: Final[dict[str, str]] = {"return": "quote", "spread": "pip", "interval": "ms"}

_UNIT_LOCATION_SCALE: Final = frozenset({"loc", "scale"})
_MS_PER_SECOND: Final = Decimal(1000)

SYMBOLOGY_KEY: Final = "symbology"
SYMBOLS_KEY: Final = "calib:symbols"
QUANTITIES_KEY: Final = "calib:quantities"

# Every key this tool writes matches one of these, so they are also what it deletes before
# writing - which is how an instrument dropped from `tables.py` leaves the store.
KEY_PATTERNS: Final = ("calib:*", "instrument:*", SYMBOLOGY_KEY)


def _decimal_text(value: Decimal) -> str:
    """Positional (never scientific) notation without trailing zeros, so a stored value reads as a
    plain number (`466.108`, not `466.108000` or `4.66108E+2`)."""
    return format(value.normalize(), "f")


def to_stored(value: float, unit: str, pip_size: Decimal) -> str:
    """`value` in code units -> its stored decimal string in `unit`."""
    exact = Decimal(repr(value))
    if unit == "pip":
        exact = exact / pip_size
    elif unit == "ms":
        exact = exact * _MS_PER_SECOND
    elif unit not in ("quote", "dimensionless"):
        raise ValueError(f"unknown unit {unit!r}")
    return _decimal_text(exact)


@dataclass(frozen=True)
class Keyspace:
    """The complete set of keys one seeding writes, by Redis type."""

    strings: dict[str, str] = field(default_factory=dict)
    hashes: dict[str, dict[str, str]] = field(default_factory=dict)
    sets: dict[str, frozenset[str]] = field(default_factory=dict)


def _fit_keys(keyspace: Keyspace, venue: str, quantity: str, fit: Fit, pip_size: Decimal) -> None:
    names = PARAM_NAMES[fit.family]
    if len(names) != len(fit.params):
        raise ValueError(
            f"{venue} {quantity}: {fit.family} takes {len(names)} parameters, got {len(fit.params)}"
        )
    prefix = f"calib:{venue}:{quantity}"
    keyspace.strings[f"{prefix}:family"] = fit.family
    keyspace.strings[f"{prefix}:param_count"] = str(len(names))
    for n, (name, value) in enumerate(zip(names, fit.params, strict=True)):
        unit = QUANTITIES[quantity] if name in _UNIT_LOCATION_SCALE else "dimensionless"
        keyspace.hashes[f"{prefix}:param:{n}"] = {
            "name": name,
            "value": to_stored(value, unit, pip_size),
            "unit": unit,
        }


def build_keyspace(
    symbology: Mapping[str, str] = SYMBOLOGY,
    instruments: Mapping[str, Instrument] = INSTRUMENTS,
    fits: Mapping[str, Mapping[str, Fit]] | None = None,
) -> Keyspace:
    """The keyspace for the given tables (by default, `tables.py`'s)."""
    fits = (
        fits
        if fits is not None
        else {
            "return": RETURN_FITS,
            "spread": SPREAD_FITS,
            "interval": INTERVAL_FITS,
        }
    )
    venues = set(symbology)
    for name, table in (("instruments", instruments), *fits.items()):
        if set(table) != venues:
            raise ValueError(
                f"{name} table covers {sorted(set(table) ^ venues)} differently from SYMBOLOGY"
            )

    keyspace = Keyspace()
    keyspace.hashes[SYMBOLOGY_KEY] = dict(symbology)
    keyspace.sets[SYMBOLS_KEY] = frozenset(venues)
    keyspace.sets[QUANTITIES_KEY] = frozenset(fits)
    for venue in sorted(venues):
        instrument = instruments[venue]
        keyspace.hashes[f"instrument:{venue}"] = {
            "pip_size": _decimal_text(instrument.pip_size),
            "quote_currency": instrument.quote_currency,
            "initial_price": _decimal_text(instrument.initial_price),
        }
        for quantity, table in fits.items():
            _fit_keys(keyspace, venue, quantity, table[venue], instrument.pip_size)
    return keyspace


class _Pipeline(Protocol):
    def delete(self, *names: str) -> object: ...
    def set(self, name: str, value: str) -> object: ...
    def hset(self, name: str, *, mapping: Mapping[str, str]) -> object: ...
    def sadd(self, name: str, *values: str) -> object: ...
    def execute(self) -> list[object]: ...


class SeedClient(Protocol):
    """The slice of a synchronous `redis.Redis` the seeder uses."""

    def scan_iter(self, match: str) -> Iterable[bytes | str]: ...
    def pipeline(self, transaction: bool = True) -> _Pipeline: ...


def seed(client: SeedClient, keyspace: Keyspace | None = None) -> Keyspace:
    """Replace the calibration keyspace in `client` with `keyspace` (default: `tables.py`'s).

    Existing calibration keys are found first, then deleted and the new set written inside one
    `MULTI`/`EXEC`, so a concurrent reader sees either the previous seeding or this one - never a
    mix - and keys belonging to an instrument no longer in the tables are gone afterwards.
    """
    keyspace = keyspace if keyspace is not None else build_keyspace()
    existing: set[str] = set()
    for pattern in KEY_PATTERNS:
        for key in client.scan_iter(match=pattern):
            existing.add(key.decode() if isinstance(key, bytes) else key)

    pipe = client.pipeline(transaction=True)
    if existing:
        pipe.delete(*sorted(existing))
    for name, value in keyspace.strings.items():
        pipe.set(name, value)
    for name, mapping in keyspace.hashes.items():
        pipe.hset(name, mapping=mapping)
    for name, members in keyspace.sets.items():
        pipe.sadd(name, *sorted(members))
    pipe.execute()
    return keyspace
