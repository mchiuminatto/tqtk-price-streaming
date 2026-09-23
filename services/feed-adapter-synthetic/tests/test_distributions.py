"""Verification that each hand-rolled `random.Random`-based sampler in `distributions.py` actually
reproduces its family's known mean/variance/median - these are non-trivial statistical transforms
(inverse-CDF, ratio-of-normals, etc.), not one-liners, so each gets checked against its textbook
formula rather than trusted by inspection. (Per-symbol completeness is the calibration store's job
now - see `test_calibration_store.py`.)
"""

from __future__ import annotations

import math
import random
import statistics

import pytest
from feed_adapter_synthetic.distributions import Distribution

_N = 200_000
_SEED = 12345


def _samples(dist: Distribution, n: int = _N) -> list[float]:
    rng = random.Random(_SEED)
    return [dist.sample(rng) for _ in range(n)]


# --- each family matches its textbook mean/variance/median ---------------------------------------


def test_constant_always_returns_the_same_value():
    dist = Distribution("constant", (0.00042,))
    rng = random.Random(0)

    assert all(dist.sample(rng) == 0.00042 for _ in range(100))


def test_normal_matches_its_mean_and_stdev():
    dist = Distribution("normal", (0.0002, 0.0001))
    values = _samples(dist)

    assert statistics.fmean(values) == pytest.approx(0.0002, abs=3 * 0.0001 / _N**0.5)
    assert statistics.pstdev(values) == pytest.approx(0.0001, rel=0.05)


def test_laplace_matches_its_mean_and_variance():
    loc, scale = 0.0, 0.0002
    dist = Distribution("laplace", (loc, scale))
    values = _samples(dist)

    expected_stdev = scale * math.sqrt(2)  # Var(Laplace) = 2*b^2
    assert statistics.fmean(values) == pytest.approx(loc, abs=3 * expected_stdev / _N**0.5)
    assert statistics.pstdev(values) == pytest.approx(expected_stdev, rel=0.05)


def test_student_t_matches_its_mean_and_variance():
    df, loc, scale = 8.0, 0.0, 1.0  # df > 2, so variance is finite
    dist = Distribution("student_t", (df, loc, scale))
    values = _samples(dist)

    expected_variance = scale**2 * df / (df - 2)
    expected_stdev = math.sqrt(expected_variance)
    assert statistics.fmean(values) == pytest.approx(loc, abs=3 * expected_stdev / _N**0.5)
    assert statistics.pstdev(values) == pytest.approx(expected_stdev, rel=0.05)


def test_gamma_matches_its_mean_and_variance():
    shape, loc, scale = 2.0, 0.0, 0.5
    dist = Distribution("gamma", (shape, loc, scale))
    values = _samples(dist)

    expected_mean = loc + shape * scale
    expected_stdev = math.sqrt(shape) * scale
    assert statistics.fmean(values) == pytest.approx(expected_mean, rel=0.02)
    assert statistics.pstdev(values) == pytest.approx(expected_stdev, rel=0.05)


def test_lognormal_matches_its_mean_and_variance():
    sigma, loc, scale = 0.5, 0.0, 1.0
    dist = Distribution("lognormal", (sigma, loc, scale))
    values = _samples(dist)

    expected_mean = loc + scale * math.exp(sigma**2 / 2)
    expected_variance = scale**2 * (math.exp(sigma**2) - 1) * math.exp(sigma**2)
    expected_stdev = math.sqrt(expected_variance)
    assert statistics.fmean(values) == pytest.approx(expected_mean, rel=0.02)
    assert statistics.pstdev(values) == pytest.approx(expected_stdev, rel=0.05)


def test_weibull_min_matches_its_mean():
    shape, loc, scale = 2.0, 0.0, 1.0
    dist = Distribution("weibull_min", (shape, loc, scale))
    values = _samples(dist)

    expected_mean = loc + scale * math.gamma(1 + 1 / shape)
    assert statistics.fmean(values) == pytest.approx(expected_mean, rel=0.02)


def test_loglogistic_matches_its_median_and_mean():
    shape, loc, scale = 4.0, 0.0, 1.0  # shape > 1, so the mean is finite
    dist = Distribution("loglogistic", (shape, loc, scale))
    values = _samples(dist)

    expected_median = loc + scale  # F^-1(0.5) = scale * (0.5/0.5)^(1/shape) = scale
    expected_mean = loc + scale * (math.pi / shape) / math.sin(math.pi / shape)
    assert statistics.median(values) == pytest.approx(expected_median, rel=0.02)
    assert statistics.fmean(values) == pytest.approx(expected_mean, rel=0.02)


def test_unknown_family_raises():
    dist = Distribution("not_a_real_family", (1.0,))

    with pytest.raises(ValueError, match="no sampler registered"):
        dist.sample(random.Random(0))
