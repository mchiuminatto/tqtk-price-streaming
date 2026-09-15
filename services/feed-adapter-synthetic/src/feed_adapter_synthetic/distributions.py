"""Per-symbol return/spread/tick-interval distributions - the model `docs/synthetic-price.md`
specifies and `calibration.py`/`generator.py` consume.

Each symbol's family and parameters here are the frozen result of an offline MLE fit (`scipy`,
run outside this package) ranked by AIC over several candidate families per quantity, documented
in full - including *why* each family won over the others - in `docs/tick-distributions.md`
(returns), `docs/spread-distributions.md` (spread), and `docs/tick-interval-distributions.md`
(tick interval). This module is the executable form of those three tables: every `Distribution`
below names the same family and carries the same `scipy.stats.<family>.fit`-ordered parameters
printed there, so any entry can be cross-checked directly against its source doc.

Unlike `calibration.py`'s `initial_price`/`price_increment` (re-derived from `data/*.parquet` at
every adapter startup), this mapping does not change with the sample data - it's the answer to a
model-*selection* question ("which family"), not a per-run statistic, and re-deriving it at
startup would need `scipy`/`numpy` as a runtime dependency for a fit that only needs to happen
once per symbol. The trade-off, deliberately accepted: adding a new symbol now needs a row added
here too, not just a sample file dropped under `data/` (unlike `symbols.discover_symbols`) - if
that drift becomes a real problem, the fix is running the offline fit script again and updating
the three docs and this module together, not inventing a runtime fallback.

The three tables themselves are private; callers reach a symbol's fitted distributions through
`return_distribution`/`spread_distribution`/`interval_distribution`, which raise a `ValueError`
naming the missing quantity and its source doc rather than a bare `KeyError`. That keeps the
"which symbols are registered" question (`registered_symbols`) and the "add a row here" error in
the module that owns the data, instead of restated at every call site.

`sample()` implements every family with `random.Random` alone, via standard transforms, so the
feed itself never needs `scipy`/`numpy` at runtime - only the offline fitting step (outside this
package) does.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = [
    "Distribution",
    "interval_distribution",
    "registered_symbols",
    "return_distribution",
    "spread_distribution",
]


@dataclass(frozen=True, slots=True)
class Distribution:
    """One fitted family plus its `scipy.stats`-ordered parameters - see module docstring."""

    family: str
    params: tuple[float, ...]

    def sample(self, rng: random.Random) -> float:
        try:
            sampler = _SAMPLERS[self.family]
        except KeyError:
            raise ValueError(
                f"no sampler registered for distribution family {self.family!r}"
            ) from None
        return sampler(rng, self.params)


def _sample_constant(rng: random.Random, params: tuple[float, ...]) -> float:
    """Always returns `params[0]`, ignoring `rng` - a fixed point-mass, not a fit to any symbol.

    Exists for tests that want a fully deterministic `_RandomWalk` without approximating "zero
    variance" through some other family's degenerate parameter values.
    """
    (value,) = params
    return value


def _sample_normal(rng: random.Random, params: tuple[float, ...]) -> float:
    loc, scale = params
    return rng.gauss(loc, scale)


def _sample_laplace(rng: random.Random, params: tuple[float, ...]) -> float:
    loc, scale = params
    # Laplace(loc, scale) is the difference of two i.i.d. Exponential(1/scale) variables - a
    # standard construction, avoids implementing the inverse-CDF transform by hand.
    return loc + rng.expovariate(1.0 / scale) - rng.expovariate(1.0 / scale)


def _sample_student_t(rng: random.Random, params: tuple[float, ...]) -> float:
    df, loc, scale = params
    # T = Z / sqrt(V/df), Z ~ N(0,1), V ~ chi2(df) = Gamma(df/2, 2) - the standard construction
    # of a (non-central) Student-t variable from a normal and a chi-squared draw.
    z = rng.gauss(0.0, 1.0)
    v = rng.gammavariate(df / 2.0, 2.0)
    return loc + scale * z / math.sqrt(v / df)


def _sample_gamma(rng: random.Random, params: tuple[float, ...]) -> float:
    shape, loc, scale = params
    return loc + rng.gammavariate(shape, scale)


def _sample_lognormal(rng: random.Random, params: tuple[float, ...]) -> float:
    sigma, loc, scale = params
    # scipy's lognorm(s, loc, scale): ln(X - loc) ~ N(ln(scale), s).
    return loc + rng.lognormvariate(math.log(scale), sigma)


def _sample_weibull_min(rng: random.Random, params: tuple[float, ...]) -> float:
    shape, loc, scale = params
    # `random.weibullvariate(alpha, beta)` names its args (scale, shape) - the opposite order
    # from scipy's `weibull_min(c=shape, scale=scale)`; `alpha=scale, beta=shape` below is
    # deliberate, not a typo.
    return loc + rng.weibullvariate(scale, shape)


def _sample_loglogistic(rng: random.Random, params: tuple[float, ...]) -> float:
    shape, loc, scale = params
    # Log-logistic (Fisk) has a closed-form inverse CDF: F^-1(u) = scale * (u/(1-u))^(1/shape).
    u = rng.random()
    return loc + scale * (u / (1.0 - u)) ** (1.0 / shape)


_SAMPLERS: dict[str, Callable[[random.Random, tuple[float, ...]], float]] = {
    "constant": _sample_constant,
    "normal": _sample_normal,
    "laplace": _sample_laplace,
    "student_t": _sample_student_t,
    "gamma": _sample_gamma,
    "lognormal": _sample_lognormal,
    "weibull_min": _sample_weibull_min,
    "loglogistic": _sample_loglogistic,
}


# Returns: docs/tick-distributions.md. All 17 symbols reject Normal in favor of Laplace or
# Student-t (params: laplace=(loc, scale), student_t=(df, loc, scale)).
_RETURN_DISTRIBUTIONS: dict[str, Distribution] = {
    "AAPLUSUSD": Distribution("laplace", (0.0, 0.0171211)),
    "ARKQUSUSD": Distribution("laplace", (0.0, 0.00535162)),
    "AUDJPY": Distribution("laplace", (0.0, 0.00124485)),
    "AUDUSD": Distribution("student_t", (7.5965, -9.41761e-08, 1.24265e-05)),
    "EURGBP": Distribution("laplace", (0.0, 9.34852e-06)),
    "EURJPY": Distribution("laplace", (0.0, 0.00128839)),
    "EURUSD": Distribution("laplace", (0.0, 1.06012e-05)),
    "GBPJPY": Distribution("laplace", (0.0, 0.00144856)),
    "GBPUSD": Distribution("laplace", (0.0, 1.1239e-05)),
    "NZDJPY": Distribution("laplace", (0.0, 0.00121249)),
    "NZDUSD": Distribution("laplace", (0.0, 9.8903e-06)),
    "USA500IDXUSD": Distribution("student_t", (52.279, 0.000169648, 0.212719)),
    "USATECHIDXUSD": Distribution("student_t", (3.26087, -1.04606e-05, 0.293872)),
    "USDCAD": Distribution("laplace", (0.0, 1.07115e-05)),
    "USDCHF": Distribution("laplace", (0.0, 9.42445e-06)),
    "USDCNH": Distribution("laplace", (0.0, 1.3132e-05)),
    "USDJPY": Distribution("student_t", (2.83953, -1.02753e-05, 0.00118423)),
}

# Spread (Ask - Bid): docs/spread-distributions.md. Strictly positive, so fit against a positive-
# support family with `loc` pinned per symbol (params: shape/a, loc, scale in every case).
_SPREAD_DISTRIBUTIONS: dict[str, Distribution] = {
    "AAPLUSUSD": Distribution("weibull_min", (1.26909, 0.0159993, 0.0480636)),
    "ARKQUSUSD": Distribution("loglogistic", (3.5914, 0.0159993, 0.0770927)),
    "AUDJPY": Distribution("gamma", (0.28141, 0.00399969, 0.00877235)),
    "AUDUSD": Distribution("loglogistic", (8.20604, 9.99784e-06, 7.18048e-05)),
    "EURGBP": Distribution("loglogistic", (2.68703, 2.99981e-05, 2.9065e-05)),
    "EURJPY": Distribution("loglogistic", (4.56321, 0.000999762, 0.00682481)),
    "EURUSD": Distribution("gamma", (0.422882, 9.99855e-06, 4.92226e-05)),
    "GBPJPY": Distribution("loglogistic", (6.06016, 0.00399953, 0.011678)),
    "GBPUSD": Distribution("loglogistic", (4.96566, 9.99859e-06, 5.31058e-05)),
    "NZDJPY": Distribution("weibull_min", (0.819898, 0.00399951, 0.00407185)),
    "NZDUSD": Distribution("loglogistic", (8.45018, 9.99717e-06, 7.96001e-05)),
    "USA500IDXUSD": Distribution("loglogistic", (33.4842, 0.000995785, 0.503615)),
    "USATECHIDXUSD": Distribution("weibull_min", (3.04864, 0.501999, 0.808555)),
    "USDCAD": Distribution("loglogistic", (5.00189, 3.99967e-05, 6.66425e-05)),
    "USDCHF": Distribution("loglogistic", (4.50507, 3.99978e-05, 3.75168e-05)),
    "USDCNH": Distribution("loglogistic", (4.71096, 4.99974e-05, 0.000106743)),
    "USDJPY": Distribution("weibull_min", (1.17157, 0.000999852, 0.00344205)),
}

# Tick interval (seconds between consecutive ticks): docs/tick-interval-distributions.md.
# Exponential (a memoryless Poisson-process baseline) never wins for any symbol - tick arrivals
# are consistently more bursty/clustered than that (params: sigma/shape, loc, scale).
_INTERVAL_DISTRIBUTIONS: dict[str, Distribution] = {
    "AAPLUSUSD": Distribution("lognormal", (1.0246, -0.0130015, 0.238192)),
    "ARKQUSUSD": Distribution("lognormal", (1.35399, -0.0130048, 0.512815)),
    "AUDJPY": Distribution("loglogistic", (0.902244, 0.0496834, 0.139578)),
    "AUDUSD": Distribution("lognormal", (1.53141, -0.00024015, 0.513165)),
    "EURGBP": Distribution("lognormal", (1.59028, -0.00026072, 0.573163)),
    "EURJPY": Distribution("lognormal", (1.86543, 0.0497566, 0.0825586)),
    "EURUSD": Distribution("lognormal", (1.50353, -0.000242662, 0.466108)),
    "GBPJPY": Distribution("loglogistic", (0.994504, 0.0496651, 0.0782325)),
    "GBPUSD": Distribution("lognormal", (1.36625, -0.000190256, 0.338735)),
    "NZDJPY": Distribution("lognormal", (2.02345, 0.0493953, 0.129613)),
    "NZDUSD": Distribution("lognormal", (1.40681, -0.000311268, 0.405276)),
    "USA500IDXUSD": Distribution("lognormal", (1.3852, 0.0436958, 0.4625)),
    "USATECHIDXUSD": Distribution("loglogistic", (3.02613, 0.0436933, 0.128119)),
    "USDCAD": Distribution("lognormal", (1.47981, -0.000246778, 0.38011)),
    "USDCHF": Distribution("lognormal", (1.36899, -0.000414271, 0.410013)),
    "USDCNH": Distribution("lognormal", (1.35722, -0.000203472, 0.383959)),
    "USDJPY": Distribution("loglogistic", (0.962625, 0.0498518, 0.124719)),
}


def _lookup(
    table: Mapping[str, Distribution], symbol: str, quantity: str, doc: str
) -> Distribution:
    """Fetch `symbol`'s entry from one table, or say which row is missing and where to add it.

    A bare `KeyError` here would only name the symbol; the actionable part is which of the three
    quantities lacks a fit and which doc the replacement row comes from - so this raises
    `ValueError` with both, per this package's "raise rather than guess" convention (see
    `symbols.find_data_dir`/`find_symbol_file`).
    """
    try:
        return table[symbol]
    except KeyError:
        raise ValueError(
            f"no fitted {quantity} distribution for symbol {symbol!r} - add it per {doc}"
        ) from None


def return_distribution(symbol: str) -> Distribution:
    """`symbol`'s fitted log-return distribution - see `docs/tick-distributions.md`."""
    return _lookup(_RETURN_DISTRIBUTIONS, symbol, "return", "docs/tick-distributions.md")


def spread_distribution(symbol: str) -> Distribution:
    """`symbol`'s fitted Ask-Bid spread distribution - see `docs/spread-distributions.md`."""
    return _lookup(_SPREAD_DISTRIBUTIONS, symbol, "spread", "docs/spread-distributions.md")


def interval_distribution(symbol: str) -> Distribution:
    """`symbol`'s fitted tick-interval distribution - see `docs/tick-interval-distributions.md`."""
    return _lookup(
        _INTERVAL_DISTRIBUTIONS, symbol, "tick interval", "docs/tick-interval-distributions.md"
    )


def registered_symbols() -> frozenset[str]:
    """Symbols carrying all three fitted distributions.

    Intersection, not union: a symbol fitted for only one or two quantities cannot be generated,
    so reporting it as registered would hide exactly the half-added-symbol case this is meant to
    surface.
    """
    return frozenset(
        _RETURN_DISTRIBUTIONS.keys() & _SPREAD_DISTRIBUTIONS.keys() & _INTERVAL_DISTRIBUTIONS.keys()
    )
