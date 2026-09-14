# Tick Return Distributions — Per-Symbol Best Fit

`docs/synthetic-price.md` originally modeled every instrument's returns as `N(μ_I, σ_I)` — one
fixed family (Normal) for all 17 symbols. This note checked that assumption empirically against
the real sample data (`data/*.parquet`) rather than assuming it, and the result below is why
`docs/synthetic-price.md` now specifies a per-symbol fitted return distribution instead (see
Status).

## Method

For each symbol: load `Bid`, take consecutive differences (the same `returns` series
`calibration.py` fits), then fit five candidate continuous distributions by MLE — Normal,
Student-t, Laplace, Logistic, Cauchy — and rank them by AIC (log-likelihood penalized for
parameter count; lower is better), computed over the full sample with parameters fit on a
200k-row subsample for speed on the larger files (`USATECHIDXUSD` runs to ~1.7M ticks). Cross-checked
with a KS test. Run with `scipy`/`numpy` in a throwaway virtualenv — not a project dependency.

## Result

Every symbol rejects Normal decisively. Normal isn't even runner-up anywhere except
`USA500IDXUSD`.

| Symbol | n | exc. kurtosis | skew | Best fit | Parameters | 2nd best | ΔAIC (2nd − best) |
|---|---:|---:|---:|---|---|---|---:|
| AAPLUSUSD | 288,047 | 66.6 | 0.32 | **Laplace** | μ=0, b=0.0171211 | Student-t | 8,004 |
| ARKQUSUSD | 93,117 | 11646.7 | -52.71 | **Laplace** | μ=0, b=0.00535162 | Cauchy | 11,933 |
| AUDJPY | 584,957 | 75.2 | -0.66 | **Laplace** | μ=0, b=0.00124485 | Logistic | 36,705 |
| AUDUSD | 218,297 | 412.3 | -3.42 | **Student-t** | ν=7.5965, loc=-9.42e-08, scale=1.24265e-05 | Logistic | 6,144 |
| EURGBP | 180,742 | 61.6 | -0.02 | **Laplace** | μ=0, b=9.34852e-06 | Student-t | 4,482 |
| EURJPY | 986,759 | 83.3 | -1.25 | **Laplace** | μ=0, b=0.00128839 | Student-t | 68,855 |
| EURUSD | 248,691 | 1686.6 | -11.78 | **Laplace** | μ=0, b=1.06012e-05 | Logistic | 3,395 |
| GBPJPY | 1,130,395 | 88.6 | -0.64 | **Laplace** | μ=0, b=0.00144856 | Student-t | 12,335 |
| GBPUSD | 371,474 | 3328.4 | -17.24 | **Laplace** | μ=0, b=1.1239e-05 | Logistic | 17,434 |
| NZDJPY | 548,062 | 3357.1 | 0.87 | **Laplace** | μ=0, b=0.00121249 | Student-t | 41,117 |
| NZDUSD | 305,084 | 1706.2 | -3.02 | **Laplace** | μ=0, b=9.8903e-06 | Logistic | 16,017 |
| USA500IDXUSD | 290,418 | 576.3 | -5.02 | **Student-t** | ν=52.279, loc=0.000169648, scale=0.212719 | Normal | 18,505 |
| USATECHIDXUSD | 1,725,162 | 2350.7 | -5.47 | **Student-t** | ν=3.26087, loc=-1.05e-05, scale=0.293872 | Laplace | 52,609 |
| USDCAD | 289,367 | 957.7 | 2.31 | **Laplace** | μ=0, b=1.07115e-05 | Student-t | 10,814 |
| USDCHF | 324,754 | 845.1 | 5.29 | **Laplace** | μ=0, b=9.42445e-06 | Student-t | 13,254 |
| USDCNH | 330,340 | 136.4 | -0.61 | **Laplace** | μ=0, b=1.3132e-05 | Student-t | 18,935 |
| USDJPY | 650,415 | 996.8 | 3.95 | **Student-t** | ν=2.83953, loc=-1.03e-05, scale=0.00118423 | Laplace | 3,538 |

14/17 symbols pick Laplace, the other 3 (`AUDUSD`, `USA500IDXUSD`, `USATECHIDXUSD`, `USDJPY`) pick
Student-t.

## Reading the parameters

- **Laplace(μ, b)**: peak at `μ`; PDF `∝ exp(-|x-μ|/b)`. `b` is the mean absolute deviation —
  analogous to (but smaller in magnitude than) a Gaussian's `σ`. Every fitted `μ` landed at
  exactly 0: the median return in each series is a flat tick (no price change), which is where
  Laplace's MLE location estimator (the sample median) lands.
- **Student-t(ν, loc, scale)**: `ν` (degrees of freedom) controls tail weight — lower `ν` means
  fatter tails; `ν → ∞` converges to Normal. `USATECHIDXUSD` and `USDJPY` have very low `ν`
  (≈2.8–3.3, near the boundary where variance is barely finite); `USA500IDXUSD`'s `ν ≈ 52` is
  nearly Normal-like, consistent with it being the one symbol where Normal placed 2nd.

## Caveat: `ARKQUSUSD`'s extreme kurtosis is a real gap, not a data bug

Checked before trusting it: `ARKQUSUSD`'s three largest returns all land exactly on the
`16:59:59 → next-day 10:30:00` overnight-close boundary — a genuine session gap, not a corrupted
row. This is the underlying reason Normal loses everywhere: real tick series are long quiet
stretches punctuated by occasional larger jumps (session gaps, news), which is a
mixture-like/heavy-tailed process, not i.i.d. Gaussian noise applied uniformly to every tick.
Matches Cont, R. (2001), *"Empirical properties of asset returns: stylized facts and statistical
issues"* — leptokurtosis is close to universal in financial returns.

## Status

**Adopted.** `services/feed-adapter-synthetic/src/feed_adapter_synthetic/distributions.py` is the
executable form of the table above: `RETURN_DISTRIBUTIONS` carries each symbol's exact family and
parameters, and `SymbolCalibration.return_distribution`/`_RandomWalk.next_quote` sample from it
(via hand-rolled `random.Random`-based transforms - no `scipy`/`numpy` at runtime). `docs/synthetic-price.md`
was updated to specify this per-symbol model in place of the old fixed `N(μ_I, σ_I)`.
