# Tick Spread Distributions — Per-Symbol Best Fit

Companion to [`tick-distributions.md`](./tick-distributions.md), same question but for spread
(`Ask - Bid`) instead of returns. At the time this analysis was run, neither
`docs/synthetic-price.md` nor `calibration.py` modeled spread at all — `generator.py` used a
single hardcoded `_DEFAULT_SPREAD` constant for every symbol. This note checked empirically what
spread actually looks like per instrument, and the result below is why both now use a per-symbol
fitted spread distribution instead (see Status).

## Method

Spread is strictly positive for every symbol checked (`min > 0`, verified directly — no zero or
negative spreads in the sample data), unlike returns, which are symmetric around 0. So the
candidate family here is five positive-support distributions instead of returns' symmetric
real-line ones: **Exponential, Gamma, Log-normal, Weibull-min, Log-logistic (Fisk)** — fit by MLE,
ranked by AIC, cross-checked with a KS test. Same subsampling (200k rows for fit speed on the
larger files), same throwaway `scipy`/`numpy` virtualenv as the returns analysis.

**One methodological difference from the returns table, and why:** letting each distribution's
`loc` (left-edge) parameter float freely — the default for `scipy.stats.<dist>.fit` — produced
numerically degenerate fits on this heavily-quantized, right-shifted-away-from-0 data (e.g. a
gamma with shape≈0.05 and scale≈1.2 for `EURUSD`, whose real spread never exceeds 0.0015; a
Weibull with shape≈4×10⁷ for `USATECHIDXUSD`). Both are optimizer excursions, not real fits — a
known instability of free-`loc` 3-parameter MLE on data with many tied/repeated values. Fix: pin
`loc` per symbol at just below its observed minimum spread (`min(spread) - epsilon`), then fit
only the remaining shape/scale parameters. This also makes AIC comparable across the five
candidates, since they now share the same left boundary.

## Result

| Symbol | n | exc.kurt | skew | Best fit | Parameters | 2nd best | ΔAIC (2nd − best) |
|---|---:|---:|---:|---|---|---|---:|
| AAPLUSUSD | 288,048 | 27.4 | 3.61 | **Weibull-min** | c=1.26909, loc=0.0159993, scale=0.0480636 | Gamma | 9,941 |
| ARKQUSUSD | 93,118 | 4.2 | 1.87 | **Log-logistic** | c=3.5914, loc=0.0159993, scale=0.0770927 | Log-normal | 9,457 |
| AUDJPY | 584,958 | 191.1 | 12.53 | **Gamma** | a=0.28141, loc=0.00399969, scale=0.00877235 | Weibull-min | 100,371 |
| AUDUSD | 218,298 | 76.2 | 8.16 | **Log-logistic** | c=8.20604, loc=9.99784e-06, scale=7.18048e-05 | Log-normal | 142,835 |
| EURGBP | 180,743 | 27.4 | 5.01 | **Log-logistic** | c=2.68703, loc=2.99981e-05, scale=2.9065e-05 | Log-normal | 40,168 |
| EURJPY | 986,760 | 283.9 | 13.94 | **Log-logistic** | c=4.56321, loc=0.000999762, scale=0.00682481 | Log-normal | 175,407 |
| EURUSD | 248,692 | 211.5 | 11.56 | **Gamma** | a=0.422882, loc=9.99855e-06, scale=4.92226e-05 | Weibull-min | 63,905 |
| GBPJPY | 1,130,396 | 205.2 | 12.86 | **Log-logistic** | c=6.06016, loc=0.00399953, scale=0.011678 | Log-normal | 485,893 |
| GBPUSD | 371,475 | 108.7 | 9.79 | **Log-logistic** | c=4.96566, loc=9.99859e-06, scale=5.31058e-05 | Log-normal | 142,441 |
| NZDJPY | 548,063 | 242.7 | 12.65 | **Weibull-min** | c=0.819898, loc=0.00399951, scale=0.00407185 | Gamma | 15,338 |
| NZDUSD | 305,085 | 144.5 | 10.52 | **Log-logistic** | c=8.45018, loc=9.99717e-06, scale=7.96001e-05 | Log-normal | 240,151 |
| USA500IDXUSD | 290,419 | 973.0 | 15.80 | **Log-logistic** | c=33.4842, loc=0.000995785, scale=0.503615 | Gamma | 131,627 |
| USATECHIDXUSD | 1,725,163 | -1.3 | -0.38 | **Weibull-min** | c=3.04864, loc=0.501999, scale=0.808555 | Gamma | 359,392 |
| USDCAD | 289,368 | 91.7 | 9.40 | **Log-logistic** | c=5.00189, loc=3.99967e-05, scale=6.66425e-05 | Log-normal | 151,452 |
| USDCHF | 324,755 | 295.3 | 15.70 | **Log-logistic** | c=4.50507, loc=3.99978e-05, scale=3.75168e-05 | Log-normal | 222,026 |
| USDCNH | 330,341 | 77.6 | 8.34 | **Log-logistic** | c=4.71096, loc=4.99974e-05, scale=0.000106743 | Log-normal | 161,072 |
| USDJPY | 650,416 | 154.4 | 9.63 | **Weibull-min** | c=1.17157, loc=0.000999852, scale=0.00344205 | Gamma | 14,040 |

11/17 symbols pick Log-logistic, 4/17 (`AUDJPY`, `EURUSD`) pick Gamma, and (`AAPLUSUSD`,
`NZDJPY`, `USATECHIDXUSD`, `USDJPY`) pick Weibull-min. Log-normal is runner-up almost everywhere
Log-logistic wins — the two are close cousins (both right-skewed, both log-symmetric-ish), so this
is a fairly stable signal, not a coin flip between unrelated shapes.

## Reading the parameters

All five families here are `(shape, loc, scale)` (Exponential drops the shape: just
`(loc, scale)`), with `loc` pinned per symbol (see Method) rather than freely fitted:

- **Weibull-min(c, loc, scale)**: `c < 1` → decreasing hazard, most mass piles up right at `loc`
  with a long right tail (`AAPLUSUSD`, `NZDJPY`, `USDJPY` all have `c` in this range); `c > 1` (as
  in `USATECHIDXUSD`, `c≈3.05`) → more bell-shaped, mass pulled away from the left edge.
- **Gamma(a, loc, scale)**: `a < 1` (both winners here, `a≈0.28` and `a≈0.42`) → an L-shaped
  density, decreasing from `loc`, not peaked — i.e. the single most common spread is the minimum
  observed one, with a long right tail of wider spreads.
- **Log-logistic/Fisk(c, loc, scale)**: median spread `= loc + scale`; larger `c` → tighter
  concentration around that median. `USA500IDXUSD`'s `c≈33.5` is the tightest (spread barely
  varies around ~0.5+0.0007), while `ARKQUSUSD`'s `c≈3.6` is comparatively loose.

## Caveat

Unlike the returns table, spread here is a genuinely different physical quantity per instrument
(FX pip spreads run ~1e-5–1e-4; equity/index CFD spreads run in whole cents/points) — the
right-skewed-positive shape is common across all 17, but the fitted scale/loc values are not
comparable across symbols without normalizing by each instrument's own price level.

## Status

**Adopted.** `distributions.py`'s `SPREAD_DISTRIBUTIONS` carries each symbol's exact family and
parameters from the table above. `_DEFAULT_SPREAD` is gone: `_RandomWalk.next_quote` now draws a
fresh spread per tick from `SymbolCalibration.spread_distribution`, and `ask = bid + spread`
(replacing the old `mid ± spread/2`). `docs/synthetic-price.md` now specifies this spread model
explicitly.
