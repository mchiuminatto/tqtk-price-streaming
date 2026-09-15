"""Symbol-set discovery from the sample tick data.

The `synthetic-feed` capability sources the deployed symbol set from `data/*.parquet` rather than
a hardcoded list (design.md, "Symbol set sourced from the sample data, not a hardcoded list"), so
adding a symbol is "add its sample file," not a second config edit that can drift out of sync with
the fidelity-calibration sample set.

Mirrors `tqtk_common.testing.find_contracts_dir`'s walk-up-from-caller pattern: `data/` is a
repository-root asset, not something shipped inside a service image, so discovery runs from a
checkout (where a test or a build step invokes it), not from an installed wheel.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

__all__ = ["discover_symbols", "find_data_dir", "find_symbol_file"]

# The sample files are named `{SYMBOL}_Ticks_{start}_{end}.parquet`; only the prefix matters here.
_SYMBOL_FROM_FILENAME: Final = re.compile(r"^([A-Z0-9]{2,20})_")


def find_data_dir(start: Path | None = None) -> Path:
    """The repository's `data/` directory, found by walking up from `start`.

    Raises rather than guessing: a discovery step that silently found no directory and returned
    an empty symbol set would start the adapter successfully while publishing nothing.
    """
    for candidate in (start or Path(__file__).resolve()).parents:
        data_dir = candidate / "data"
        if data_dir.is_dir():
            return data_dir
    raise FileNotFoundError(
        f"no data/ directory above {start or __file__}; "
        "symbol discovery runs from a repository checkout, not from an installed wheel"
    )


def discover_symbols(data_dir: Path | None = None) -> tuple[str, ...]:
    """The configured symbol set: one entry per distinct symbol prefix among `data/*.parquet`.

    Sorted for a stable, deterministic order rather than whatever order the filesystem lists
    entries in. Raises on an unparseable filename or an empty directory rather than silently
    narrowing the symbol set.
    """
    directory = data_dir or find_data_dir()
    symbols: set[str] = set()
    for path in sorted(directory.glob("*.parquet")):
        match = _SYMBOL_FROM_FILENAME.match(path.name)
        if match is None:
            raise ValueError(f"{path.name!r} does not start with a symbol prefix (SYMBOL_...)")
        symbols.add(match.group(1))
    if not symbols:
        raise FileNotFoundError(f"no *.parquet sample files found under {directory}")
    return tuple(sorted(symbols))


def find_symbol_file(symbol: str, data_dir: Path | None = None) -> Path:
    """The sample `data/*.parquet` file for `symbol` - the source per-instrument calibration
    (`calibration.py`) fits from.

    Raises rather than guessing: a missing sample must fail adapter startup, not silently fall
    back to an uncalibrated default.
    """
    directory = data_dir or find_data_dir()
    matches = sorted(directory.glob(f"{symbol}_*.parquet"))
    if not matches:
        raise FileNotFoundError(f"no sample file for symbol {symbol!r} under {directory}")
    return matches[0]
