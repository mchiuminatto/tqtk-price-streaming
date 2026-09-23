"""`Distribution` - one fitted family plus its parameters - and sampling from it, for the model
`docs/synthetic-price.md` specifies and `generator.py` consumes.

Which family and parameters each symbol uses is not decided here: every symbol's return, spread
and tick-interval distributions are read from the calibration store at startup
(`calibration_store.py`), and the values themselves - with the offline fit that produced them and
why each family won - live only in the calibration seed script,
`deploy/calibration/calibration.redis`. Parameters are in `scipy.stats.<family>.fit` order and in code units (price units for returns and
spreads, seconds for intervals).

`sample()` implements every family with `random.Random` alone, via standard transforms, so the
feed itself never needs `scipy`/`numpy` at runtime - only the offline fitting step (outside this
package) does. `SUPPORTED_FAMILIES` is what a calibration source may name.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "SUPPORTED_FAMILIES",
    "Distribution",
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

# The family names `Distribution.sample` can draw from - what a calibration source must name.
SUPPORTED_FAMILIES: Final = frozenset(_SAMPLERS)
