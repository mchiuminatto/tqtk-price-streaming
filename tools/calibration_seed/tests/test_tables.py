"""Task 1.1: the seeding tool holds every seeded value and needs no sample data to do it."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tools.calibration_seed.tables import (
    INSTRUMENTS,
    INTERVAL_FITS,
    PARAM_NAMES,
    RETURN_FITS,
    SPREAD_FITS,
    SYMBOLOGY,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def test_tool_imports_without_pyarrow() -> None:
    # `sys.modules["pyarrow"] = None` makes any `import pyarrow` raise, so this fails if the tool
    # (or anything it imports) reaches for it.
    code = (
        "import sys; sys.modules['pyarrow'] = None; "
        "import tools.calibration_seed.__main__, tools.calibration_seed.keyspace"
    )
    subprocess.run([sys.executable, "-c", code], cwd=_REPO_ROOT, check=True)


def test_every_table_covers_the_same_instruments() -> None:
    venues = set(SYMBOLOGY)
    assert len(venues) == 17
    for table in (INSTRUMENTS, RETURN_FITS, SPREAD_FITS, INTERVAL_FITS):
        assert set(table) == venues


def test_every_fit_names_all_its_parameters() -> None:
    for table in (RETURN_FITS, SPREAD_FITS, INTERVAL_FITS):
        for venue, fit in table.items():
            assert len(PARAM_NAMES[fit.family]) == len(fit.params), venue


def test_quote_currency_is_the_part_after_the_slash() -> None:
    for venue, instrument in INSTRUMENTS.items():
        assert instrument.quote_currency == venue.split("/")[1]
