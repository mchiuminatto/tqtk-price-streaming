"""Reads every symbol's `SymbolCalibration` from the Redis calibration store.

The store - its key names, stored units and discovery sets - is the `calibration-store`
capability; its only writer is the seed script `deploy/calibration/calibration.redis`, applied by
the `calibration-seeder` container with `redis-cli`, which is also the single source of truth for
its values. This module is the read side:

- `calib:symbols` lists venue symbols (`EUR/USD`); `symbology` maps each to the file symbol
  (`EURUSD`) used on the wire. The mapping is never derived by string manipulation.
- `instrument:<venue>` holds `pip_size` (the minimum price increment), `quote_currency` and
  `initial_price`.
- `calib:<venue>:<quantity>:family`/`param_count`/`param:<n>` hold one fitted distribution per
  quantity (`return`, `spread`, `interval`), each parameter tagged with a `unit`.

Stored units are converted to code units by each parameter's `unit` alone, never by family name:
`pip` x `pip_size` (spreads), `ms` / 1000 (intervals), `quote` and `dimensionless` unchanged. The
arithmetic is `Decimal` on the stored decimal strings, so it is an exact decimal shift - no binary
floating-point error is introduced before the final conversion to `float`.

`load` validates the whole keyspace before returning and raises `CalibrationError` naming the
symbol and key on the first problem - no fallback, no partial result, per this package's "raise
rather than guess" convention. That includes checking every distribution against what its family's
sampler will do with it (`distributions.FAMILY_PARAMETERS`: the parameter count, each parameter's
name at its position, and a positive value for every scale and shape parameter), so a calibration
that loads is one the generator can draw from. It costs three pipelined round trips however many
symbols there are.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from types import TracebackType
from typing import Any, Final, Protocol, Self

from .calibration import SymbolCalibration
from .distributions import FAMILY_PARAMETERS, UNBOUNDED_PARAMETERS, Distribution

__all__ = ["QUANTITIES", "CalibrationError", "CalibrationStore"]

SYMBOLOGY_KEY: Final = "symbology"
SYMBOLS_KEY: Final = "calib:symbols"
QUANTITIES_KEY: Final = "calib:quantities"

# The three quantities a `SymbolCalibration` needs, in its field order.
QUANTITIES: Final = ("return", "spread", "interval")

_INSTRUMENT_FIELDS: Final = ("pip_size", "quote_currency", "initial_price")
_PARAM_FIELDS: Final = ("name", "value", "unit")
_UNITS: Final = frozenset({"quote", "pip", "ms", "dimensionless"})

_SEEDER_HINT: Final = (
    "run the `calibration-seeder` Compose service "
    "(it applies `deploy/calibration/calibration.redis`)"
)


class CalibrationError(ValueError):
    """The calibration store is missing, incomplete or malformed - the adapter must not start."""


class _Pipeline(Protocol):
    """The slice of a `redis.asyncio` pipeline `CalibrationStore` uses."""

    def smembers(self, name: str) -> Any: ...
    def hgetall(self, name: str) -> Any: ...
    def get(self, name: str) -> Any: ...
    async def execute(self) -> list[Any]: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> Any: ...


class _RedisClient(Protocol):
    """The slice of `redis.asyncio.Redis` `CalibrationStore` uses - so a test fake qualifies too."""

    def pipeline(self, transaction: bool = ...) -> _Pipeline: ...


def _text(value: bytes | str, *, key: str) -> str:
    if isinstance(value, str):
        return value
    try:
        return value.decode()
    except UnicodeDecodeError:
        raise CalibrationError(f"`{key}` holds a value that is not UTF-8: {value!r}") from None


def _decode_hash(raw: Mapping[bytes | str, bytes | str], *, key: str) -> dict[str, str]:
    return {_text(k, key=key): _text(v, key=key) for k, v in raw.items()}


def _decimal(text: str, *, key: str, field: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise CalibrationError(f"`{key}` field {field!r} is not a number: {text!r}") from None
    if not value.is_finite():
        raise CalibrationError(f"`{key}` field {field!r} is not finite: {text!r}")
    return value


def _is_power_of_ten(value: Decimal) -> bool:
    # `normalize` strips trailing zeros, so a power of ten is left with the single digit 1.
    return value > 0 and value.normalize().as_tuple().digits == (1,)


def _to_code_units(value: Decimal, unit: str, pip_size: Decimal) -> float:
    if unit == "pip":
        value = value * pip_size
    elif unit == "ms":
        value = value.scaleb(-3)
    return float(value)


def _parse_instrument(
    venue: str, raw: Mapping[bytes | str, bytes | str]
) -> tuple[Decimal, Decimal]:
    """`instrument:<venue>`'s `pip_size` and `initial_price`, validated."""
    key = f"instrument:{venue}"
    instrument = _decode_hash(raw, key=key)
    for field in _INSTRUMENT_FIELDS:
        if not instrument.get(field):
            raise CalibrationError(f"{venue!r}: `{key}` has no {field!r} field")
    pip_size = _decimal(instrument["pip_size"], key=key, field="pip_size")
    # `generator._RandomWalk` rounds prices by quantizing to `pip_size`'s exponent, which is an
    # exact rounding to multiples of it only when it is a power of ten.
    if not _is_power_of_ten(pip_size):
        raise CalibrationError(
            f"{venue!r}: `{key}` field 'pip_size' must be a positive power of ten, "
            f"not {instrument['pip_size']!r}"
        )
    initial_price = _decimal(instrument["initial_price"], key=key, field="initial_price")
    if initial_price <= 0:
        raise CalibrationError(
            f"{venue!r}: `{key}` field 'initial_price' must be positive, "
            f"not {instrument['initial_price']!r}"
        )
    return pip_size, initial_price


def _parse_head(
    venue: str, quantity: str, raw_family: bytes | str | None, raw_count: bytes | str | None
) -> tuple[str, int]:
    """One quantity's `family` and `param_count`, validated against the family's parameters."""
    prefix = f"calib:{venue}:{quantity}"
    if raw_family is None:
        raise CalibrationError(f"{venue!r}: `{prefix}:family` is missing")
    family = _text(raw_family, key=f"{prefix}:family")
    if family not in FAMILY_PARAMETERS:
        raise CalibrationError(
            f"{venue!r}: `{prefix}:family` names {family!r}, which the adapter "
            f"cannot sample (supported: {sorted(FAMILY_PARAMETERS)})"
        )
    if raw_count is None:
        raise CalibrationError(f"{venue!r}: `{prefix}:param_count` is missing")
    text = _text(raw_count, key=f"{prefix}:param_count")
    try:
        count = int(text)
    except ValueError:
        raise CalibrationError(
            f"{venue!r}: `{prefix}:param_count` is not an integer: {text!r}"
        ) from None
    expected = FAMILY_PARAMETERS[family]
    if count != len(expected):
        raise CalibrationError(
            f"{venue!r}: `{prefix}:param_count` is {count}, but {family!r} takes "
            f"{len(expected)} parameters {expected}"
        )
    return family, count


class CalibrationStore:
    """Loads `SymbolCalibration`s from the calibration keyspace - see module docstring."""

    __slots__ = ("_redis",)

    def __init__(self, redis: _RedisClient) -> None:
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

        venues = sorted(_text(v, key=SYMBOLS_KEY) for v in raw_symbols)
        if not venues:
            raise CalibrationError(
                f"calibration store is not seeded: `{SYMBOLS_KEY}` is empty or absent - "
                f"{_SEEDER_HINT}"
            )
        quantities = {_text(q, key=QUANTITIES_KEY) for q in raw_quantities}
        missing = [q for q in QUANTITIES if q not in quantities]
        if missing:
            raise CalibrationError(f"`{QUANTITIES_KEY}` lacks {missing} - {_SEEDER_HINT}")

        symbology = _decode_hash(raw_symbology, key=SYMBOLOGY_KEY)
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
            instruments[venue] = _parse_instrument(venue, next(replies))
            for quantity in QUANTITIES:
                raw_family, raw_count = next(replies), next(replies)
                heads[venue, quantity] = _parse_head(venue, quantity, raw_family, raw_count)
        return instruments, heads

    async def _read_params(
        self, venues: list[str], heads: Mapping[tuple[str, str], tuple[str, int]]
    ) -> dict[tuple[str, str], list[tuple[Decimal, str]]]:
        """Round trip 3: every parameter hash, plus the one past `param_count`, which must be
        absent - so a hash left over beyond the family's parameters is caught as well as a missing
        one."""
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
                family, count = heads[venue, quantity]
                prefix = f"calib:{venue}:{quantity}"
                values: list[tuple[Decimal, str]] = []
                for n, expected_name in enumerate(FAMILY_PARAMETERS[family]):
                    key = f"{prefix}:param:{n}"
                    param = _decode_hash(next(replies), key=key)
                    if not param:
                        raise CalibrationError(
                            f"{venue!r}: `{prefix}:param_count` is {count} but `{key}` is missing"
                        )
                    for field in _PARAM_FIELDS:
                        if not param.get(field):
                            raise CalibrationError(f"{venue!r}: `{key}` has no {field!r} field")
                    if param["name"] != expected_name:
                        raise CalibrationError(
                            f"{venue!r}: `{key}` is named {param['name']!r}, but {family!r} "
                            f"takes {expected_name!r} at position {n}"
                        )
                    unit = param["unit"]
                    if unit not in _UNITS:
                        raise CalibrationError(
                            f"{venue!r}: `{key}` has unknown unit {unit!r} "
                            f"(expected one of {sorted(_UNITS)})"
                        )
                    value = _decimal(param["value"], key=key, field="value")
                    if expected_name not in UNBOUNDED_PARAMETERS and value <= 0:
                        raise CalibrationError(
                            f"{venue!r}: `{key}` ({expected_name!r}) must be positive, "
                            f"not {param['value']!r}"
                        )
                    values.append((value, unit))
                if _decode_hash(next(replies), key=f"{prefix}:param:{count}"):
                    raise CalibrationError(
                        f"{venue!r}: `{prefix}:param_count` is {count} but "
                        f"`{prefix}:param:{count}` exists"
                    )
                params[venue, quantity] = values
        return params
