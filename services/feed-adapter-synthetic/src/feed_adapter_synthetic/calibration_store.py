"""Reads every symbol's `SymbolCalibration` from the Redis calibration store.

The store - its key names, stored units and discovery sets - is the `calibration-store`
capability; the seeding tool (`tools/calibration_seed`) is its only writer and the single source of
truth for its values. This module is the read side:

- `calib:symbols` lists venue symbols (`EUR/USD`); `symbology` maps each to the file symbol
  (`EURUSD`) used on the wire. The mapping is never derived by string manipulation.
- `instrument:<venue>` holds `pip_size` (the minimum price increment), `quote_currency` and
  `initial_price`.
- `calib:<venue>:<quantity>:family`/`param_count`/`param:<n>` hold one fitted distribution per
  quantity (`return`, `spread`, `interval`), each parameter tagged with a `unit`.

Stored units are converted to code units by each parameter's `unit` alone, never by family name:
`pip` x `pip_size` (spreads), `ms` / 1000 (intervals), `quote` and `dimensionless` unchanged. The
arithmetic is `Decimal` on the stored decimal strings, so it is an exact decimal shift and yields
exactly the float the seeder started from.

`load` validates the whole keyspace before returning and raises `CalibrationError` naming the
symbol and key on the first problem - no fallback, no partial result, per this package's "raise
rather than guess" convention. It costs three pipelined round trips however many symbols there are.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Final

from redis.asyncio import Redis

from .calibration import SymbolCalibration
from .distributions import SUPPORTED_FAMILIES, Distribution

__all__ = ["QUANTITIES", "CalibrationError", "CalibrationStore"]

SYMBOLOGY_KEY: Final = "symbology"
SYMBOLS_KEY: Final = "calib:symbols"
QUANTITIES_KEY: Final = "calib:quantities"

# The three quantities a `SymbolCalibration` needs, in its field order.
QUANTITIES: Final = ("return", "spread", "interval")

_INSTRUMENT_FIELDS: Final = ("pip_size", "quote_currency", "initial_price")
_PARAM_FIELDS: Final = ("name", "value", "unit")
_UNITS: Final = frozenset({"quote", "pip", "ms", "dimensionless"})

_SEEDER_HINT: Final = "run the calibration seeder (`python -m tools.calibration_seed`)"


class CalibrationError(ValueError):
    """The calibration store is missing, incomplete or malformed - the adapter must not start."""


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _decode_hash(raw: Mapping[bytes | str, bytes | str]) -> dict[str, str]:
    return {_text(k): _text(v) for k, v in raw.items()}


def _decimal(text: str, *, key: str, field: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise CalibrationError(f"`{key}` field {field!r} is not a number: {text!r}") from None
    if not value.is_finite():
        raise CalibrationError(f"`{key}` field {field!r} is not finite: {text!r}")
    return value


def _to_code_units(value: Decimal, unit: str, pip_size: Decimal) -> float:
    if unit == "pip":
        value = value * pip_size
    elif unit == "ms":
        value = value.scaleb(-3)
    return float(value)


class CalibrationStore:
    """Loads `SymbolCalibration`s from the calibration keyspace - see module docstring."""

    __slots__ = ("_redis",)

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def load(self) -> dict[str, SymbolCalibration]:
        """Every calibrated symbol's `SymbolCalibration`, keyed by file symbol in sorted order.

        Raises `CalibrationError` if the store is unseeded or any symbol's entry is incomplete.
        """
        venues, symbology = await self._discover()
        instruments, fit_heads = await self._read_heads(venues)
        params = await self._read_params(venues, fit_heads)

        calibrations: dict[str, SymbolCalibration] = {}
        for venue in venues:
            pip_size, initial_price = instruments[venue]
            distributions = {
                quantity: Distribution(
                    fit_heads[venue, quantity][0],
                    tuple(
                        _to_code_units(value, unit, pip_size)
                        for value, unit in params[venue, quantity]
                    ),
                )
                for quantity in QUANTITIES
            }
            calibrations[symbology[venue]] = SymbolCalibration(
                initial_price=initial_price,
                price_increment=pip_size,
                return_distribution=distributions["return"],
                spread_distribution=distributions["spread"],
                interval_distribution=distributions["interval"],
            )
        return dict(sorted(calibrations.items()))

    async def _discover(self) -> tuple[list[str], dict[str, str]]:
        """Round trip 1: the discovery sets and the symbology mapping."""
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.smembers(SYMBOLS_KEY)
            pipe.smembers(QUANTITIES_KEY)
            pipe.hgetall(SYMBOLOGY_KEY)
            raw_symbols, raw_quantities, raw_symbology = await pipe.execute()

        venues = sorted(_text(v) for v in raw_symbols)
        if not venues:
            raise CalibrationError(
                f"calibration store is not seeded: `{SYMBOLS_KEY}` is empty or absent - "
                f"{_SEEDER_HINT}"
            )
        quantities = {_text(q) for q in raw_quantities}
        missing = [q for q in QUANTITIES if q not in quantities]
        if missing:
            raise CalibrationError(f"`{QUANTITIES_KEY}` lacks {missing} - {_SEEDER_HINT}")

        symbology = _decode_hash(raw_symbology)
        seen: dict[str, str] = {}
        for venue in venues:
            file_symbol = symbology.get(venue)
            if not file_symbol:
                raise CalibrationError(f"{venue!r}: no entry in `{SYMBOLOGY_KEY}`")
            if file_symbol in seen:
                raise CalibrationError(
                    f"{venue!r} and {seen[file_symbol]!r} both map to {file_symbol!r} in "
                    f"`{SYMBOLOGY_KEY}`"
                )
            seen[file_symbol] = venue
        return venues, symbology

    async def _read_heads(
        self, venues: list[str]
    ) -> tuple[dict[str, tuple[Decimal, Decimal]], dict[tuple[str, str], tuple[str, int]]]:
        """Round trip 2: each instrument hash, and each quantity's family and parameter count."""
        async with self._redis.pipeline(transaction=False) as pipe:
            for venue in venues:
                pipe.hgetall(f"instrument:{venue}")
                for quantity in QUANTITIES:
                    pipe.get(f"calib:{venue}:{quantity}:family")
                    pipe.get(f"calib:{venue}:{quantity}:param_count")
            replies = iter(await pipe.execute())

        instruments: dict[str, tuple[Decimal, Decimal]] = {}
        heads: dict[tuple[str, str], tuple[str, int]] = {}
        for venue in venues:
            key = f"instrument:{venue}"
            instrument = _decode_hash(next(replies))
            for field in _INSTRUMENT_FIELDS:
                if not instrument.get(field):
                    raise CalibrationError(f"{venue!r}: `{key}` has no {field!r} field")
            pip_size = _decimal(instrument["pip_size"], key=key, field="pip_size")
            if pip_size <= 0:
                raise CalibrationError(f"{venue!r}: `{key}` field 'pip_size' must be positive")
            initial_price = _decimal(instrument["initial_price"], key=key, field="initial_price")
            instruments[venue] = (pip_size, initial_price)

            for quantity in QUANTITIES:
                prefix = f"calib:{venue}:{quantity}"
                raw_family, raw_count = next(replies), next(replies)
                if raw_family is None:
                    raise CalibrationError(f"{venue!r}: `{prefix}:family` is missing")
                family = _text(raw_family)
                if family not in SUPPORTED_FAMILIES:
                    raise CalibrationError(
                        f"{venue!r}: `{prefix}:family` names {family!r}, which the adapter "
                        f"cannot sample (supported: {sorted(SUPPORTED_FAMILIES)})"
                    )
                if raw_count is None:
                    raise CalibrationError(f"{venue!r}: `{prefix}:param_count` is missing")
                try:
                    count = int(_text(raw_count))
                except ValueError:
                    count = -1
                if count < 0:
                    raise CalibrationError(
                        f"{venue!r}: `{prefix}:param_count` is not a non-negative integer: "
                        f"{_text(raw_count)!r}"
                    )
                heads[venue, quantity] = (family, count)
        return instruments, heads

    async def _read_params(
        self, venues: list[str], heads: Mapping[tuple[str, str], tuple[str, int]]
    ) -> dict[tuple[str, str], list[tuple[Decimal, str]]]:
        """Round trip 3: every parameter hash, plus the one past `param_count`, which must be
        absent - so a count lower than the hashes present is caught as well as a higher one."""
        async with self._redis.pipeline(transaction=False) as pipe:
            for venue in venues:
                for quantity in QUANTITIES:
                    _, count = heads[venue, quantity]
                    for n in range(count + 1):
                        pipe.hgetall(f"calib:{venue}:{quantity}:param:{n}")
            replies = iter(await pipe.execute())

        params: dict[tuple[str, str], list[tuple[Decimal, str]]] = {}
        for venue in venues:
            for quantity in QUANTITIES:
                _, count = heads[venue, quantity]
                prefix = f"calib:{venue}:{quantity}"
                values: list[tuple[Decimal, str]] = []
                for n in range(count):
                    key = f"{prefix}:param:{n}"
                    param = _decode_hash(next(replies))
                    if not param:
                        raise CalibrationError(
                            f"{venue!r}: `{prefix}:param_count` is {count} but `{key}` is missing"
                        )
                    for field in _PARAM_FIELDS:
                        if not param.get(field):
                            raise CalibrationError(f"{venue!r}: `{key}` has no {field!r} field")
                    unit = param["unit"]
                    if unit not in _UNITS:
                        raise CalibrationError(
                            f"{venue!r}: `{key}` has unknown unit {unit!r} "
                            f"(expected one of {sorted(_UNITS)})"
                        )
                    values.append((_decimal(param["value"], key=key, field="value"), unit))
                if _decode_hash(next(replies)):
                    raise CalibrationError(
                        f"{venue!r}: `{prefix}:param_count` is {count} but "
                        f"`{prefix}:param:{count}` exists"
                    )
                params[venue, quantity] = values
        return params
