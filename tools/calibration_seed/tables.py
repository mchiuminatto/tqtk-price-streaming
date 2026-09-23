"""Every value the calibration store is seeded with - the single source of truth for them.

Nothing else in the repository restates these numbers: not the service (which reads them from
Redis at startup), not `docs/`. Changing an instrument's calibration is an edit here and a re-run
of the seeder, nothing more.

Keyed by venue symbol (`EUR/USD`). The file symbol (`EURUSD`) - the form used in stream names and
on the `Tick` wire contract - comes only from `SYMBOLOGY`; readers never derive it by stripping
punctuation.

Distribution parameters are in *code units* - returns and spreads in price units of the quote
currency, tick intervals in seconds - and in `scipy.stats.<family>.fit` order. The seeder converts
them to stored units (see `keyspace.py`); keeping the tables in code units means each row reads
exactly as the fit printed it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

__all__ = [
    "INSTRUMENTS",
    "INTERVAL_FITS",
    "PARAM_NAMES",
    "RETURN_FITS",
    "SPREAD_FITS",
    "SYMBOLOGY",
    "Fit",
    "Instrument",
]


@dataclass(frozen=True, slots=True)
class Fit:
    """One fitted distribution: a family name and its parameters, in code units and fit order."""

    family: str
    params: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Instrument:
    """One instrument's scalar metadata - see `INSTRUMENTS`."""

    pip_size: Decimal
    quote_currency: str
    initial_price: Decimal


# Parameter names per family, in fit order. `loc` and `scale` carry their quantity's unit; every
# other name is a shape parameter and is dimensionless (see `keyspace.py`).
PARAM_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "laplace": ("loc", "scale"),
    "student_t": ("df", "loc", "scale"),
    "gamma": ("a", "loc", "scale"),
    "lognormal": ("s", "loc", "scale"),
    "weibull_min": ("c", "loc", "scale"),
    "loglogistic": ("c", "loc", "scale"),
}

# Venue symbol -> file symbol. FX pairs use `BASE/QUOTE`; the CFDs use the venue's
# `<ticker>.<market>/<quote>` notation.
SYMBOLOGY: Final[dict[str, str]] = {
    "AAPL.US/USD": "AAPLUSUSD",
    "ARKQ.US/USD": "ARKQUSUSD",
    "AUD/JPY": "AUDJPY",
    "AUD/USD": "AUDUSD",
    "EUR/GBP": "EURGBP",
    "EUR/JPY": "EURJPY",
    "EUR/USD": "EURUSD",
    "GBP/JPY": "GBPJPY",
    "GBP/USD": "GBPUSD",
    "NZD/JPY": "NZDJPY",
    "NZD/USD": "NZDUSD",
    "USA500.IDX/USD": "USA500IDXUSD",
    "USATECH.IDX/USD": "USATECHIDXUSD",
    "USD/CAD": "USDCAD",
    "USD/CHF": "USDCHF",
    "USD/CNH": "USDCNH",
    "USD/JPY": "USDJPY",
}

# Per-instrument metadata.
#
# `initial_price` is the first `Bid` of each symbol's tick sample (`data/*.parquet`, week of
# 2026-08-31) - the Bid, not the Bid/Ask mid, since averaging the two adds sub-grain noise.
#
# `pip_size` is the instrument's minimum price increment: the fewest decimal places every `Bid` in
# that sample round-trips through unchanged. Rounding to fewer decimals than a price's true
# precision changes it; rounding to at least that many is a no-op, so the smallest such count is
# the sample's quoting grain - robust to a price with trailing zeros (`1.10000`), which survives
# rounding at any count and so never lowers the answer. Two groups fall out: every FX pair quoted
# against JPY, and all four CFDs, quote to the 3rd decimal; every other FX pair to the 5th.
#
# This is finer than the classical FX "pip" (the 4th decimal on non-JPY pairs, the 2nd on JPY
# pairs): the feed quotes one digit past it - a pipette on non-JPY pairs. `pip_size` means the
# grain actually quoted, whatever it is called, and spread parameters are stored in multiples of
# it. The CFDs have no FX pip convention at all; the 3rd decimal is the broker's CFD convention,
# independent of price level (a $319 stock and a 29,290-point index both quote to 3 decimals).
#
# `quote_currency` is the part of the venue symbol after `/`.
INSTRUMENTS: Final[dict[str, Instrument]] = {
    "AAPL.US/USD": Instrument(Decimal("0.001"), "USD", Decimal("319.426")),
    "ARKQ.US/USD": Instrument(Decimal("0.001"), "USD", Decimal("121.566")),
    "AUD/JPY": Instrument(Decimal("0.001"), "JPY", Decimal("114.496")),
    "AUD/USD": Instrument(Decimal("0.00001"), "USD", Decimal("0.71659")),
    "EUR/GBP": Instrument(Decimal("0.00001"), "GBP", Decimal("0.85584")),
    "EUR/JPY": Instrument(Decimal("0.001"), "JPY", Decimal("185.215")),
    "EUR/USD": Instrument(Decimal("0.00001"), "USD", Decimal("1.15922")),
    "GBP/JPY": Instrument(Decimal("0.001"), "JPY", Decimal("216.402")),
    "GBP/USD": Instrument(Decimal("0.00001"), "USD", Decimal("1.35444")),
    "NZD/JPY": Instrument(Decimal("0.001"), "JPY", Decimal("94.556")),
    "NZD/USD": Instrument(Decimal("0.00001"), "USD", Decimal("0.59177")),
    "USA500.IDX/USD": Instrument(Decimal("0.001"), "USD", Decimal("7680.869")),
    "USATECH.IDX/USD": Instrument(Decimal("0.001"), "USD", Decimal("29290.786")),
    "USD/CAD": Instrument(Decimal("0.00001"), "CAD", Decimal("1.38902")),
    "USD/CHF": Instrument(Decimal("0.00001"), "CHF", Decimal("0.80852")),
    "USD/CNH": Instrument(Decimal("0.00001"), "CNH", Decimal("6.72294")),
    "USD/JPY": Instrument(Decimal("0.001"), "JPY", Decimal("159.773")),
}

# How the three fit tables below were produced (an offline run with `scipy`/`numpy` in a
# throwaway virtualenv - neither is a project dependency): for each symbol, five candidate
# families were fitted by MLE on a 200k-row subsample (the largest file, USATECHIDXUSD, runs to
# ~1.7M ticks), ranked by AIC computed over the full sample (log-likelihood penalized for
# parameter count; lower is better), and cross-checked with a KS test. The winner's family and
# parameters are the row. Real tick series are long quiet stretches punctuated by jumps (session
# gaps, news) - heavy-tailed, not i.i.d. Gaussian - which is why no quantity picks Normal or
# Exponential anywhere (cf. Cont, R. (2001), "Empirical properties of asset returns: stylized
# facts and statistical issues").

# Returns: consecutive `Bid` differences, in price units. Candidates: Normal, Student-t, Laplace,
# Logistic, Cauchy. Every symbol rejects Normal decisively; 13 pick Laplace, 4 pick Student-t.
#
# - Laplace(loc, scale): every fitted `loc` is exactly 0 - the median return is a flat tick, and
#   Laplace's MLE location is the sample median. `scale` is the mean absolute deviation.
# - Student-t(df, loc, scale): lower `df` means fatter tails. USATECHIDXUSD and USDJPY sit near
#   df 3 (variance barely finite); USA500IDXUSD's df ~52 is nearly Normal - the only symbol where
#   Normal even placed second.
# - ARKQUSUSD's extreme kurtosis (~11,600) is real, not a data bug: its three largest returns all
#   land on the 16:59:59 -> next-day 10:30:00 overnight-close boundary.
#
# Runner-up and AIC margin (2nd - best) per symbol, for anyone revisiting a close call:
# AAPLUSUSD Student-t 8,004; ARKQUSUSD Cauchy 11,933; AUDJPY Logistic 36,705; AUDUSD Logistic
# 6,144; EURGBP Student-t 4,482; EURJPY Student-t 68,855; EURUSD Logistic 3,395; GBPJPY Student-t
# 12,335; GBPUSD Logistic 17,434; NZDJPY Student-t 41,117; NZDUSD Logistic 16,017; USA500IDXUSD
# Normal 18,505; USATECHIDXUSD Laplace 52,609; USDCAD Student-t 10,814; USDCHF Student-t 13,254;
# USDCNH Student-t 18,935; USDJPY Laplace 3,538.
RETURN_FITS: Final[dict[str, Fit]] = {
    "AAPL.US/USD": Fit("laplace", (0.0, 0.0171211)),
    "ARKQ.US/USD": Fit("laplace", (0.0, 0.00535162)),
    "AUD/JPY": Fit("laplace", (0.0, 0.00124485)),
    "AUD/USD": Fit("student_t", (7.5965, -9.41761e-08, 1.24265e-05)),
    "EUR/GBP": Fit("laplace", (0.0, 9.34852e-06)),
    "EUR/JPY": Fit("laplace", (0.0, 0.00128839)),
    "EUR/USD": Fit("laplace", (0.0, 1.06012e-05)),
    "GBP/JPY": Fit("laplace", (0.0, 0.00144856)),
    "GBP/USD": Fit("laplace", (0.0, 1.1239e-05)),
    "NZD/JPY": Fit("laplace", (0.0, 0.00121249)),
    "NZD/USD": Fit("laplace", (0.0, 9.8903e-06)),
    "USA500.IDX/USD": Fit("student_t", (52.279, 0.000169648, 0.212719)),
    "USATECH.IDX/USD": Fit("student_t", (3.26087, -1.04606e-05, 0.293872)),
    "USD/CAD": Fit("laplace", (0.0, 1.07115e-05)),
    "USD/CHF": Fit("laplace", (0.0, 9.42445e-06)),
    "USD/CNH": Fit("laplace", (0.0, 1.3132e-05)),
    "USD/JPY": Fit("student_t", (2.83953, -1.02753e-05, 0.00118423)),
}

# Spread (Ask - Bid), in price units. Strictly positive in every sample, so the candidates are
# five positive-support families: Exponential, Gamma, Log-normal, Weibull-min, Log-logistic
# (Fisk). 11 pick Log-logistic, 2 Gamma, 4 Weibull-min; Log-normal is runner-up almost everywhere
# Log-logistic wins - close cousins, so a stable signal rather than a coin flip.
#
# `loc` is pinned per symbol just below its observed minimum spread rather than fitted freely:
# free-`loc` MLE on this heavily quantized data produced degenerate optimizer excursions (a Gamma
# with shape ~0.05 for EURUSD, a Weibull with shape ~4e7 for USATECHIDXUSD). Pinning it also gives
# all five candidates the same left edge, so their AICs are comparable.
#
# - Weibull-min(c, loc, scale): c < 1 piles mass at `loc` with a long right tail; c > 1
#   (USATECHIDXUSD, ~3.05) is bell-shaped, pulled away from the edge.
# - Gamma(a, loc, scale): both winners have a < 1 - an L-shaped density, so the most common
#   spread is the minimum one.
# - Log-logistic(c, loc, scale): median = loc + scale; larger c is tighter. USA500IDXUSD's c ~33.5
#   is the tightest, ARKQUSUSD's ~3.6 comparatively loose.
#
# Spread is a different physical quantity per instrument (FX ~1e-5 to 1e-4, CFDs whole cents or
# points), so these values are not comparable across symbols without normalizing by price level.
#
# Runner-up and AIC margin: AAPLUSUSD Gamma 9,941; ARKQUSUSD Log-normal 9,457; AUDJPY Weibull-min
# 100,371; AUDUSD Log-normal 142,835; EURGBP Log-normal 40,168; EURJPY Log-normal 175,407; EURUSD
# Weibull-min 63,905; GBPJPY Log-normal 485,893; GBPUSD Log-normal 142,441; NZDJPY Gamma 15,338;
# NZDUSD Log-normal 240,151; USA500IDXUSD Gamma 131,627; USATECHIDXUSD Gamma 359,392; USDCAD
# Log-normal 151,452; USDCHF Log-normal 222,026; USDCNH Log-normal 161,072; USDJPY Gamma 14,040.
SPREAD_FITS: Final[dict[str, Fit]] = {
    "AAPL.US/USD": Fit("weibull_min", (1.26909, 0.0159993, 0.0480636)),
    "ARKQ.US/USD": Fit("loglogistic", (3.5914, 0.0159993, 0.0770927)),
    "AUD/JPY": Fit("gamma", (0.28141, 0.00399969, 0.00877235)),
    "AUD/USD": Fit("loglogistic", (8.20604, 9.99784e-06, 7.18048e-05)),
    "EUR/GBP": Fit("loglogistic", (2.68703, 2.99981e-05, 2.9065e-05)),
    "EUR/JPY": Fit("loglogistic", (4.56321, 0.000999762, 0.00682481)),
    "EUR/USD": Fit("gamma", (0.422882, 9.99855e-06, 4.92226e-05)),
    "GBP/JPY": Fit("loglogistic", (6.06016, 0.00399953, 0.011678)),
    "GBP/USD": Fit("loglogistic", (4.96566, 9.99859e-06, 5.31058e-05)),
    "NZD/JPY": Fit("weibull_min", (0.819898, 0.00399951, 0.00407185)),
    "NZD/USD": Fit("loglogistic", (8.45018, 9.99717e-06, 7.96001e-05)),
    "USA500.IDX/USD": Fit("loglogistic", (33.4842, 0.000995785, 0.503615)),
    "USATECH.IDX/USD": Fit("weibull_min", (3.04864, 0.501999, 0.808555)),
    "USD/CAD": Fit("loglogistic", (5.00189, 3.99967e-05, 6.66425e-05)),
    "USD/CHF": Fit("loglogistic", (4.50507, 3.99978e-05, 3.75168e-05)),
    "USD/CNH": Fit("loglogistic", (4.71096, 4.99974e-05, 0.000106743)),
    "USD/JPY": Fit("weibull_min", (1.17157, 0.000999852, 0.00344205)),
}

# Tick interval: seconds between consecutive ticks. Non-negative and right-skewed, so the same
# five positive-support candidates and the same pinned-`loc` fit as spread - also the textbook
# renewal-process families (Exponential the memoryless Poisson baseline, Gamma/Weibull its
# under/over-dispersed generalizations, Log-normal/Log-logistic multiplicative variability).
# 13 pick Log-normal, 4 Log-logistic. Exponential never wins and is never a close runner-up: tick
# arrivals cluster far more than a Poisson process would.
#
# - Log-normal(s, loc, scale): median = loc + scale seconds; `s` is the log-scale spread
#   (NZDJPY's ~2.02 the widest, AAPLUSUSD's ~1.02 the tightest).
# - Log-logistic(c, loc, scale): same median reading; AUDJPY/GBPJPY/USDJPY sit near c ~1, close to
#   a heavy-tailed near-Exponential gap; USATECHIDXUSD's ~3.03 is comfortably peaked.
# - AUDJPY is the one close call (AIC margin 115 over Log-normal); every other symbol is decisive.
#
# Fitted with overnight and weekend gaps left in (AAPLUSUSD/ARKQUSUSD max ~63,000 s,
# USA500IDXUSD/USATECHIDXUSD ~6,300 s): real session boundaries, not bad rows. This deliberately
# differs from the fidelity-acceptance metrics, which exclude the weekend gap.
#
# Runner-up and AIC margin: AAPLUSUSD Log-logistic 15,450; ARKQUSUSD Log-logistic 5,360; AUDJPY
# Log-normal 115; AUDUSD Log-logistic 14,837; EURGBP Log-logistic 12,572; EURJPY Log-logistic
# 1,531; EURUSD Log-logistic 16,675; GBPJPY Log-normal 11,023; GBPUSD Log-logistic 15,068; NZDJPY
# Log-logistic 3,372; NZDUSD Log-logistic 15,378; USA500IDXUSD Log-logistic 12,307; USATECHIDXUSD
# Log-normal 546,194; USDCAD Log-logistic 14,827; USDCHF Log-logistic 17,017; USDCNH Log-logistic
# 8,563; USDJPY Log-normal 3,014.
INTERVAL_FITS: Final[dict[str, Fit]] = {
    "AAPL.US/USD": Fit("lognormal", (1.0246, -0.0130015, 0.238192)),
    "ARKQ.US/USD": Fit("lognormal", (1.35399, -0.0130048, 0.512815)),
    "AUD/JPY": Fit("loglogistic", (0.902244, 0.0496834, 0.139578)),
    "AUD/USD": Fit("lognormal", (1.53141, -0.00024015, 0.513165)),
    "EUR/GBP": Fit("lognormal", (1.59028, -0.00026072, 0.573163)),
    "EUR/JPY": Fit("lognormal", (1.86543, 0.0497566, 0.0825586)),
    "EUR/USD": Fit("lognormal", (1.50353, -0.000242662, 0.466108)),
    "GBP/JPY": Fit("loglogistic", (0.994504, 0.0496651, 0.0782325)),
    "GBP/USD": Fit("lognormal", (1.36625, -0.000190256, 0.338735)),
    "NZD/JPY": Fit("lognormal", (2.02345, 0.0493953, 0.129613)),
    "NZD/USD": Fit("lognormal", (1.40681, -0.000311268, 0.405276)),
    "USA500.IDX/USD": Fit("lognormal", (1.3852, 0.0436958, 0.4625)),
    "USATECH.IDX/USD": Fit("loglogistic", (3.02613, 0.0436933, 0.128119)),
    "USD/CAD": Fit("lognormal", (1.47981, -0.000246778, 0.38011)),
    "USD/CHF": Fit("lognormal", (1.36899, -0.000414271, 0.410013)),
    "USD/CNH": Fit("lognormal", (1.35722, -0.000203472, 0.383959)),
    "USD/JPY": Fit("loglogistic", (0.962625, 0.0498518, 0.124719)),
}
