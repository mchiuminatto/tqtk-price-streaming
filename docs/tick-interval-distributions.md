# Tick Interval Distributions — Per-Symbol Best Fit

Companion to [`tick-distributions.md`](./tick-distributions.md) (returns) and
[`spread-distributions.md`](./spread-distributions.md) (`Ask - Bid`). This one covers tick
interval — the time elapsed between two consecutive ticks (`time_art[t] - time_art[t-1]`, in
seconds), which `docs/synthetic-price.md` originally modeled as one fixed `N(μ_It, σ_It)` for
every symbol and `calibration.py` fit online as `interval_mean`/`interval_stdev`. The result below
is why both now use a per-symbol fitted distribution instead (see Status).

## Method

Tick interval is non-negative and right-skewed — verified directly: one exact-zero tie
(`EURUSD`, two ticks landing in the same millisecond) and otherwise strictly positive, with a long
right tail. Same candidate family and same floc-pinning fix as
[`spread-distributions.md`](./spread-distributions.md): **Exponential, Gamma, Log-normal,
Weibull-min, Log-logistic**, fit by MLE with `loc` pinned just below each symbol's observed
minimum, ranked by AIC, cross-checked with a KS test. These five are also the textbook families
for renewal-process waiting times: Exponential is the memoryless Poisson baseline, Gamma/Weibull
are its under/over-dispersed generalizations, and Log-normal/Log-logistic capture multiplicative
variability in the gaps. Same subsampling and throwaway `scipy`/`numpy` virtualenv as the other
two analyses.

## Result

| Symbol | n | mean (s) | max (s) | exc.kurt | skew | Best fit | Parameters | 2nd best | ΔAIC |
|---|---:|---:|---:|---:|---:|---|---|---|---:|
| AAPLUSUSD | 288,047 | 1.281 | 63,001.5 | 72,005.9 | 268.34 | **Log-normal** | σ=1.0246, loc=-0.0130015, scale=0.238192 | Log-logistic | 15,450 |
| ARKQUSUSD | 93,117 | 3.962 | 63,004.9 | 23,272.9 | 152.56 | **Log-normal** | σ=1.35399, loc=-0.0130048, scale=0.512815 | Log-logistic | 5,360 |
| AUDJPY | 584,957 | 0.653 | 316.6 | 4,773.6 | 37.86 | **Log-logistic** | c=0.902244, loc=0.0496834, scale=0.139578 | Log-normal | 115 |
| AUDUSD | 218,297 | 1.760 | 240.2 | 175.4 | 8.91 | **Log-normal** | σ=1.53141, loc=-0.00024015, scale=0.513165 | Log-logistic | 14,837 |
| EURGBP | 180,742 | 2.125 | 260.7 | 267.3 | 10.73 | **Log-normal** | σ=1.59028, loc=-0.00026072, scale=0.573163 | Log-logistic | 12,572 |
| EURJPY | 986,759 | 0.389 | 243.4 | 10,575.3 | 58.32 | **Log-normal** | σ=1.86543, loc=0.0497566, scale=0.0825586 | Log-logistic | 1,531 |
| EURUSD | 248,691 | 1.545 | 242.7 | 205.0 | 9.32 | **Log-normal** | σ=1.50353, loc=-0.000242662, scale=0.466108 | Log-logistic | 16,675 |
| GBPJPY | 1,130,395 | 0.338 | 335.0 | 21,447.7 | 87.97 | **Log-logistic** | c=0.994504, loc=0.0496651, scale=0.0782325 | Log-normal | 11,023 |
| GBPUSD | 371,474 | 1.034 | 190.3 | 388.4 | 12.67 | **Log-normal** | σ=1.36625, loc=-0.000190256, scale=0.338735 | Log-logistic | 15,068 |
| NZDJPY | 548,062 | 0.697 | 604.7 | 16,888.8 | 75.99 | **Log-normal** | σ=2.02345, loc=0.0493953, scale=0.129613 | Log-logistic | 3,372 |
| NZDUSD | 305,084 | 1.259 | 311.3 | 627.6 | 14.40 | **Log-normal** | σ=1.40681, loc=-0.000311268, scale=0.405276 | Log-logistic | 15,378 |
| USA500IDXUSD | 290,418 | 1.404 | 6,304.3 | 70,283.1 | 263.00 | **Log-normal** | σ=1.3852, loc=0.0436958, scale=0.4625 | Log-logistic | 12,307 |
| USATECHIDXUSD | 1,725,162 | 0.236 | 6,306.8 | 430,164.3 | 655.45 | **Log-logistic** | c=3.02613, loc=0.0436933, scale=0.128119 | Log-normal | 546,194 |
| USDCAD | 289,367 | 1.328 | 246.8 | 449.1 | 13.03 | **Log-normal** | σ=1.47981, loc=-0.000246778, scale=0.38011 | Log-logistic | 14,827 |
| USDCHF | 324,754 | 1.183 | 414.3 | 1,608.2 | 19.97 | **Log-normal** | σ=1.36899, loc=-0.000414271, scale=0.410013 | Log-logistic | 17,017 |
| USDCNH | 330,340 | 1.163 | 203.5 | 288.8 | 11.45 | **Log-normal** | σ=1.35722, loc=-0.000203472, scale=0.383959 | Log-logistic | 8,563 |
| USDJPY | 650,415 | 0.587 | 148.3 | 532.8 | 15.52 | **Log-logistic** | c=0.962625, loc=0.0498518, scale=0.124719 | Log-normal | 3,014 |

13/17 symbols pick Log-normal, 4/17 (`AUDJPY`, `GBPJPY`, `USATECHIDXUSD`, `USDJPY`) pick
Log-logistic. Exponential — the "no memory, constant hazard" baseline that a naive Poisson-process
model of tick arrivals would assume — never wins and isn't even a competitive runner-up anywhere;
every symbol's inter-arrival times are far more bursty/clustered than a Poisson process, which is
exactly what real order-flow clustering looks like.

## Reading the parameters

- **Log-normal(σ, loc, scale)**: median interval `= loc + scale` seconds; `σ` is the log-scale
  spread — larger `σ` means a wider multiplicative range of gaps (compare `NZDJPY`'s `σ≈2.02`,
  the widest, to `AAPLUSUSD`'s `σ≈1.02`, the tightest among the Log-normal winners).
- **Log-logistic(c, loc, scale)**: same `median = loc + scale` reading as in the spread table;
  larger `c` → tighter concentration. `USATECHIDXUSD`'s `c≈3.03` is comfortably peaked, while
  `AUDJPY`/`GBPJPY`/`USDJPY` all sit near `c≈0.9–1.0` — close to the Log-logistic/Fisk shape that
  most resembles a heavy-tailed near-Exponential gap distribution.
- **`AUDJPY`'s ΔAIC to Log-normal is only 115** (versus thousands-to-hundreds-of-thousands
  everywhere else) — the only genuinely close call in this table; the other 16 symbols have a
  clear, decisive winner.

## Caveat: the extreme max/kurtosis values are overnight and weekend gaps, not bad data

`AAPLUSUSD`/`ARKQUSUSD` topping out at ~63,000s (~17.5h) and `USA500IDXUSD`/`USATECHIDXUSD` at
~6,300s (~1.75h) are the same daily market-close-to-reopen gaps identified in
[`tick-distributions.md`](./tick-distributions.md)'s `ARKQUSUSD` check — real session boundaries,
not corrupted rows. They dominate the excess-kurtosis column (up to 430,164 for
`USATECHIDXUSD`) exactly the way a handful of extreme outliers would. **This matters for
`calibration.py` as shipped**: `compute_calibration` fits `interval_mean`/`interval_stdev` from
*every* consecutive timestamp pair with no gap exclusion, so today's calibrated `σ_It` already
absorbs these overnight jumps into a single Gaussian's variance — inflating the "typical" pacing
jitter used for every tick, not just the rare one after a gap. That's different from
`add-synthetic-feed-fidelity`'s acceptance metrics, which explicitly **exclude** the weekend gap
before computing tick-volume statistics (per `Architecture-Open-Questions.md`, Thread E). This
analysis intentionally did **not** exclude the gaps either, to stay consistent with what
`compute_calibration` itself does today — the point of this table is to characterize the model
`calibration.py` is already fitting, not a cleaned-up alternative.

## Status

**Adopted, gaps included.** `distributions.py`'s `INTERVAL_DISTRIBUTIONS` carries each symbol's
exact family and parameters from the table above - fit with the overnight/weekend gaps left in,
consistent with what this doc characterizes (see the Caveat above), not with the
fidelity-acceptance spec's gap-excluded metrics. `SymbolCalibration.interval_distribution`/
`_RandomWalk.next_interval` sample from it; `docs/synthetic-price.md` now specifies this per-symbol
model in place of the old fixed `N(μ_It, σ_It)`.
