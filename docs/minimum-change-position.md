# Minimum Change Position — Per-Symbol

The minimum change position is the rightmost decimal digit present in an instrument's quoted
price — "the position in the price value at which a unit change is incorporated" per
`docs/synthetic-price.md`. This is `price_increment` in `SymbolCalibration`, used to round every
generated price to a value the real instrument could actually quote (`_RandomWalk.next_quote`).

## Method

Not a statistical fit like the other three analyses in this folder — this is the exact,
deterministic method already implemented in `calibration.py`'s `_decimal_places`: for each
symbol's real `Bid` column, find the fewest decimal places every price in the sample round-trips
through `pyarrow.compute.round` unchanged. Rounding to fewer decimals than a price's true
precision changes it; rounding to at least that many is a no-op, so the smallest `decimals` where
every price survives unchanged is the sample's true pip/tick grain — robust to trailing zeros
(e.g. `1.10000`) that would understate precision if you just reprinted the number and counted
digits. This table runs that exact production function against all 17 symbols' real
`data/*.parquet` files — no separate analysis code, no `scipy`/`numpy`, just the project's own
`pyarrow`-based calibrator logic.

## Result

| Symbol | Asset class | Sample p0 | Minimum change position | Minimum change unit | Label |
|---|---|---:|---|---:|---|
| AAPLUSUSD | Equity CFD | 319.426 | 3rd decimal | 0.001 | contract |
| ARKQUSUSD | Equity CFD | 121.566 | 3rd decimal | 0.001 | contract |
| AUDJPY | FX pair | 114.496 | 3rd decimal | 0.001 | pip |
| AUDUSD | FX pair | 0.71659 | 5th decimal | 0.00001 | pip |
| EURGBP | FX pair | 0.85584 | 5th decimal | 0.00001 | pip |
| EURJPY | FX pair | 185.215 | 3rd decimal | 0.001 | pip |
| EURUSD | FX pair | 1.15922 | 5th decimal | 0.00001 | pip |
| GBPJPY | FX pair | 216.402 | 3rd decimal | 0.001 | pip |
| GBPUSD | FX pair | 1.35444 | 5th decimal | 0.00001 | pip |
| NZDJPY | FX pair | 94.556 | 3rd decimal | 0.001 | pip |
| NZDUSD | FX pair | 0.59177 | 5th decimal | 0.00001 | pip |
| USA500IDXUSD | Index CFD | 7680.869 | 3rd decimal | 0.001 | contract |
| USATECHIDXUSD | Index CFD | 29290.786 | 3rd decimal | 0.001 | contract |
| USDCAD | FX pair | 1.38902 | 5th decimal | 0.00001 | pip |
| USDCHF | FX pair | 0.80852 | 5th decimal | 0.00001 | pip |
| USDCNH | FX pair | 6.72294 | 5th decimal | 0.00001 | pip |
| USDJPY | FX pair | 159.773 | 3rd decimal | 0.001 | pip |

Two clean groups fall out of the FX pairs, matching the classic JPY-quote convention: pairs quoted
against JPY (`AUDJPY`, `EURJPY`, `GBPJPY`, `NZDJPY`, `USDJPY`) round to the 3rd decimal; every
other FX pair rounds to the 5th. The 4 non-FX instruments (2 equity CFDs, 2 index CFDs) all land
on the 3rd decimal regardless of price level (a $319 stock and a $29,290 index both quote to
3 decimals) — a broker/CFD-feed convention, not a coincidence of these specific instruments.

## A terminology note: this is finer than the classical "pip"

The traditional FX "pip" sits one decimal coarser than what's measured here — the 4th decimal for
non-JPY pairs (e.g. `0.0001` on `EURUSD`), 2nd for JPY pairs (e.g. `0.01` on `USDJPY`). What this
sample data actually quotes to is one digit past that — a **pipette** (1/10 pip) for non-JPY pairs,
and a 3rd-decimal fractional pip for JPY pairs. `_decimal_places` finds the sample's true
finest-quoted grain, whatever that grain is called, which is why the "pip" column above reports
`0.00001`/`0.001` rather than the classical `0.0001`/`0.01` — it's not an error, it's this
particular feed's actual quoting convention, empirically measured rather than assumed.

## Status

Documentation of existing, already-shipped behavior — no code changed. This table is the output
of `calibration.py`'s current `_decimal_places` run against all 17 symbols; `compute_calibration`
computes exactly this value (as `price_increment`) for every symbol at adapter startup already.
